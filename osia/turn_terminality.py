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
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from osia.config import CONFIG_DIR


class TerminalState(str, Enum):
    SUCCESS_VERIFIED = "success_verified"
    BLOCKED_VERIFIED = "blocked_verified"
    USER_CANCELLED = "user_cancelled"
    HARD_SAFETY_BLOCK = "hard_safety_block"


TERMINAL_STATES = frozenset(item.value for item in TerminalState)


class WaitType(str, Enum):
    WAIT_PENDING_TOOL = "WAIT_PENDING_TOOL"
    WAIT_RECONCILIATION = "WAIT_RECONCILIATION"
    WAIT_PROVIDER_CONTINUATION = "WAIT_PROVIDER_CONTINUATION"
    WAIT_STRUCTURAL = "WAIT_STRUCTURAL"
_STRATEGY_ATTEMPT_EVIDENCE_STATUSES = frozenset({
    "ambiguous_effect", "failed", "inapplicable", "progressed", "stagnated",
    "stale_target", "succeeded",
})
# WOR-303: an unfinished objective whose frontier currently offers no named
# strategy always receives one more factual continuation instruction.  There is
# no replan quota: this strategy id is an instruction, never a terminal.
_REPLAN_STRATEGY_ID = "provider_replan_required"
_REPLAN_REASON_CODE = "remaining_work_requires_continuation"
# A provider invocation that raises a retryable technical error is a local
# operation failure.  The canonical objective stays alive and may
# retry/reconnect/continue; no consecutive-failure count ever terminalizes it.
#
# A blocked final may only be reserved from one *explicitly non-retryable
# provider fact* carried by the escaped provider error itself (invalid
# credentials, provider not configured, model missing/inaccessible, hard
# billing/quota restriction, proven contract incompatibility).  The fact is
# recorded once by the agent; it is never inferred from repetition.
_PROVEN_PROVIDER_FACT_BLOCKERS = frozenset({
    "provider_credentials_invalid",
    "provider_not_configured",
    "provider_model_not_found",
    "provider_model_inaccessible",
    "provider_billing_restricted",
    "provider_hard_quota_exhausted",
    "provider_contract_incompatible",
})

# Frontier strategy emitted by osia.execution_strategy when productive effects
# are complete and only the whole-objective verification proof remains.
_VERIFICATION_STRATEGY_ID = "verify_open_effect_read_only"


@dataclass(frozen=True)
class TerminalityDecision:
    action: str
    terminal_state: str = ""
    reason_code: str = ""
    next_strategy: str = ""
    postcondition: str = ""
    wait_type: str = ""

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


def live_process_identity(pid: object) -> tuple[bool, str]:
    """Return liveness plus a PID-reuse-safe process identity when available."""

    try:
        numeric_pid = int(pid or 0)
    except (TypeError, ValueError, OverflowError):
        return False, ""
    if numeric_pid <= 0:
        return False, ""
    try:
        import psutil

        process = psutil.Process(numeric_pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False, ""
        return True, f"{numeric_pid}:{process.create_time():.6f}"
    except (ImportError, OSError):
        # No reliable death proof is available. A platform-level existence
        # probe may still establish liveness without guessing an identity.
        try:
            os.kill(numeric_pid, 0)
        except ProcessLookupError:
            return False, ""
        except (PermissionError, OSError):
            return True, ""
        return True, ""
    except Exception as exc:
        try:
            import psutil

            if isinstance(exc, psutil.NoSuchProcess):
                return False, ""
        except ImportError:
            pass
        # AccessDenied and other indeterminate probes are not proof of death.
        return True, ""


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


def load_persisted_execution_binding(
    turn_id: str,
    *,
    storage_dir: str | Path | None = None,
) -> dict | None:
    """Read one atomic immutable execution binding without reopening a turn."""

    stable_turn_id = _bounded(turn_id, 256)
    if not stable_turn_id:
        return None
    root = (
        Path(storage_dir)
        if storage_dir is not None else
        Path(CONFIG_DIR) / "turn_terminality"
    )
    path = root / f"{_safe_turn_key(stable_turn_id)}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    binding = state.get("execution_binding") if isinstance(state, dict) else None
    if not isinstance(binding, dict) or set(binding) != {
        "conversation_id", "request_id", "turn_id", "run_id",
        "provider_id", "model_id", "effective_reasoning",
    }:
        return None
    safe = {
        key: _bounded(binding.get(key), maximum)
        for key, maximum in (
            ("conversation_id", 256),
            ("request_id", 256),
            ("turn_id", 256),
            ("run_id", 256),
            ("provider_id", 64),
            ("model_id", 160),
            ("effective_reasoning", 64),
        )
    }
    if (
        any(not value for value in safe.values())
        or safe["turn_id"] != stable_turn_id
        or any(str(binding.get(key) or "") != value for key, value in safe.items())
    ):
        return None
    return safe


def persisted_turn_state_exists(
    turn_id: str,
    *,
    storage_dir: str | Path | None = None,
) -> bool:
    """Return whether any durable state exists, even when it is unreadable."""

    stable_turn_id = _bounded(turn_id, 256)
    if not stable_turn_id:
        return False
    root = (
        Path(storage_dir)
        if storage_dir is not None else
        Path(CONFIG_DIR) / "turn_terminality"
    )
    path = root / f"{_safe_turn_key(stable_turn_id)}.json"
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Recovery must not classify an indeterminate canonical state as legacy.
        return True


def load_persisted_terminal_result(
    turn_id: str,
    *,
    storage_dir: str | Path | None = None,
) -> dict | None:
    """Return only a complete durable final reservation for receipt recovery."""

    stable_turn_id = _bounded(turn_id, 256)
    binding = load_persisted_execution_binding(
        stable_turn_id,
        storage_dir=storage_dir,
    )
    if binding is None:
        return None
    root = (
        Path(storage_dir)
        if storage_dir is not None else
        Path(CONFIG_DIR) / "turn_terminality"
    )
    path = root / f"{_safe_turn_key(stable_turn_id)}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    terminal_state = str(
        state.get("terminal_state") if isinstance(state, dict) else ""
    )
    reservation = (
        state.get("final_reservation") if isinstance(state, dict) else None
    )
    if terminal_state not in TERMINAL_STATES or not isinstance(reservation, dict):
        return None
    reservation_id = _bounded(reservation.get("reservation_id"), 256)
    final_text = reservation.get("final_text")
    reserved_state = str(
        reservation.get("terminal_state") or terminal_state
    )
    empty_cancelled_final = bool(
        terminal_state == TerminalState.USER_CANCELLED.value
        and isinstance(final_text, str)
        and not final_text
    )
    if (
        not reservation_id
        or not isinstance(final_text, str)
        or (not final_text.strip() and not empty_cancelled_final)
        or reserved_state != terminal_state
    ):
        return None
    result = {
        "execution_binding": binding,
        "terminal_state": terminal_state,
        "reason_code": _bounded(state.get("terminal_reason"), 160),
        "reservation_id": reservation_id,
        "final_text": final_text,
    }
    cancellation = state.get("cancellation")
    if isinstance(cancellation, dict):
        cancellation_reason = _bounded(cancellation.get("reason_code"), 160)
        superseded_by_turn_id = _bounded(
            cancellation.get("superseded_by_turn_id"), 256,
        )
        if cancellation_reason:
            result["cancellation_reason"] = cancellation_reason
        if superseded_by_turn_id:
            result["superseded_by_turn_id"] = superseded_by_turn_id
    try:
        from osia.execution_trace import validate_persisted_trace

        execution_trace = validate_persisted_trace(
            reservation.get("execution_trace")
        )
    except Exception:
        execution_trace = None
    if execution_trace is not None and all(
        str(execution_trace.get(key) or "") == str(binding.get(key) or "")
        for key in (
            "conversation_id", "request_id", "turn_id", "run_id",
            "provider_id", "model_id", "effective_reasoning",
        )
    ):
        result["execution_trace"] = execution_trace
    try:
        from osia.document_artifacts import normalize_references

        artifact_refs = normalize_references(reservation.get("artifact_refs"))
    except Exception:
        artifact_refs = []
    if artifact_refs:
        result["artifact_refs"] = artifact_refs
    return result


def _as_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _recorded_epoch(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _application_identity(value: object) -> str:
    normalized = "".join(
        char for char in str(value or "").lower()
        if char.isalnum()
    )
    for suffix in ("application", "app", "exe"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 2:
            normalized = normalized[:-len(suffix)]
            break
    return normalized


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
        semantic_objective = objective if isinstance(objective, str) else ""
        if not semantic_objective.strip():
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
                    "execution_binding": {},
                    "goal_contract": {},
                    "goal_contract_fingerprint": "",
                    "required_postconditions": postconditions,
                    "proven_postconditions": {},
                    "remaining_work": list(postconditions),
                    "typed_evidence": [],
                    "evidence_tokens": [],
                    "active_attempts": {},
                    "permission_state": "none",
                    "provider_fact_blocker": "",
                    "unknown_effects": {},
                    "current_state_refreshes": {},
                    "unknown_effect_resolutions": [],
                    "strategy_snapshot": {},
                    "strategy_frontier": [],
                    "strategy_attempt_evidence": [],
                    "strategy_attempt_revision": 0,
                    "invocations": [],
                    "evidence_revision": 0,
                    "first_causal_error": {},
                    "structural_wait_reason": "",
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
    def terminal_state(self) -> str:
        return str(self._state.get("terminal_state") or "")

    @property
    def final_reserved(self) -> bool:
        return bool(self._state.get("final_reservation"))

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    @property
    def outcomes_declared(self) -> bool:
        with self._lock:
            self._refresh()
            return bool((self._state.get("outcome_contract") or {}).get("declared"))

    def declare_outcomes(self, rows) -> dict:
        """Persist optional result notes without constraining execution."""
        from osia.outcome_contract import (
            CompletionValidationError, OutcomeContract, declare_outcomes,
        )
        with self._lock, self._process_lock():
            self._refresh()
            contract = OutcomeContract.from_dict(self._state.get("outcome_contract") or {})
            if self._state.get("final_reservation") or (self._state.get("cancellation") or {}).get("requested"):
                raise CompletionValidationError("turn_no_longer_accepts_outcome_declarations")
            declared = declare_outcomes(contract, rows)
            self._state["outcome_contract"] = declared.to_dict()
            self._persist()
            return declared.to_dict()

    def bind_outcome_contract(self, required_postconditions=()) -> None:
        """Bind results to the original objective, never to a language compiler."""
        from osia.outcome_contract import new_outcome_contract, OutcomeContract
        with self._lock, self._process_lock():
            self._refresh()
            # Cancellation may win between admission and contract binding.
            if self._state.get("terminal_state") == TerminalState.USER_CANCELLED.value:
                return
            if self._state.get("outcome_contract"):
                existing = OutcomeContract.from_dict(self._state["outcome_contract"])
                additional = new_outcome_contract(self.objective, required_postconditions)
                additions = tuple(item for item in additional.requirements if item not in existing.requirements)
                if additions:
                    if self._state.get("final_reservation"):
                        raise ValueError("completed turn cannot acquire new requirements")
                    self._state["outcome_contract"] = OutcomeContract(
                        existing.objective, existing.requirements + additions, existing.declared,
                    ).to_dict()
                    self._state["proven_postconditions"] = {}
                    self._state["remaining_work"] = ["original_objective_satisfied"]
                    self._state["completion_text"] = ""
                else:
                    self._state["outcome_contract"] = existing.to_dict()
                self._persist()
                return
            if self._state.get("goal_contract") or self._state.get("final_reservation"):
                raise ValueError("legacy turn requires its existing recovery path")
            contract = new_outcome_contract(self.objective, required_postconditions=required_postconditions)
            self._state["outcome_contract"] = contract.to_dict()
            self._state["required_postconditions"] = ["original_objective_satisfied"]
            self._state["remaining_work"] = ["original_objective_satisfied"]
            self._state["proven_postconditions"] = {}
            self._state["completion_text"] = ""
            self._persist()

    def completion_instruction(self) -> str:
        from osia.outcome_contract import completion_instruction, OutcomeContract
        state = self.snapshot()
        if not state.get("outcome_contract"):
            return ""
        return completion_instruction(OutcomeContract.from_dict(state["outcome_contract"]))

    def receipt_reference(self, result: object, attempt_id: str = "") -> str:
        token = _fingerprint(result)
        state = self.snapshot()
        if not state.get("outcome_contract"):
            return ""
        entry = next((
            item for item in reversed(state.get("typed_evidence") or [])
            if item.get("evidence_token") == token
            and (not attempt_id or item.get("attempt_id") == attempt_id)
            and item.get("outcome") in {"success_verified", "command_completed"}
            and item.get("facts")
            and not item.get("stale") and not item.get("superseded")
        ), None)
        return self._receipt_id(entry) if entry is not None else ""

    def _receipt_id(self, entry: dict) -> str:
        return _fingerprint([
            self.turn_id, entry.get("attempt_id"), entry.get("evidence_token"),
            entry.get("outcome"), entry.get("recorded_at"),
        ])

    def accept_completion(self, text: str) -> str:
        """Accept the model's final answer, retaining factual contradictions."""
        from osia.outcome_contract import (
            CompletionValidationError, OutcomeContract,
            evaluate_completion, parse_completion,
        )
        with self._lock, self._process_lock():
            self._refresh()
            contract_data = self._state.get("outcome_contract")
            if not contract_data:
                return text
            if (self._state.get("cancellation") or {}).get("requested"):
                return ""
            self._state["proven_postconditions"] = {}
            self._state["remaining_work"] = ["original_objective_satisfied"]
            self._state["completion_text"] = ""
            self._state["completion_feedback"] = ""
            try:
                proposal = parse_completion(text)
                contract = OutcomeContract.from_dict(contract_data)
                for item in self._state.get("typed_evidence") or []:
                    if any(
                        fact.get("kind") == "artifact" and not self._artifact_fact_is_current(fact)
                        for fact in item.get("facts") or [] if isinstance(fact, dict)
                    ):
                        item["stale"] = True
                receipts = {
                    self._receipt_id(item): {
                        **item, "turn_id": self.turn_id,
                    }
                    for item in self._state.get("typed_evidence") or []
                    if item.get("evidence_token")
                }
                assessment = evaluate_completion(
                    contract, proposal,
                    receipts, turn_id=self.turn_id,
                )
                self._state["outcome_contract"] = assessment.contract.to_dict()
                self._state["completion_errors"] = list(assessment.errors)
                # Optional notes never prove facts or veto a new model request.
                # A factual contradiction is returned to the same model as
                # continuation feedback instead of becoming a terminal blocker.
                if assessment.complete:
                    self._state["remaining_work"] = []
                    self._state["completion_text"] = proposal.answer
                    self._state["completion_feedback"] = ""
                else:
                    self._state["remaining_work"] = ["original_objective_satisfied"]
                    self._state["completion_text"] = ""
                    self._state["completion_feedback"] = assessment.factual_report
                self._persist()
                return self._state["completion_text"]
            except CompletionValidationError as exc:
                self._state["completion_errors"] = [str(exc)]
                self._persist()
                return ""

    @staticmethod
    def _artifact_fact(path_value: object) -> dict | None:
        if not isinstance(path_value, str) or not path_value:
            return None
        try:
            from osia.outcome_contract import normalize_fact_path
            path = Path(normalize_fact_path(path_value))
            exists = path.exists()
            properties = {"exists": exists}
            if exists and path.is_file():
                before = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    return None
                properties["content_sha256"] = digest.hexdigest()
            return {"kind": "artifact", "target": {"path": str(path)}, "properties": properties}
        except (OSError, ValueError):
            return None

    @classmethod
    def _artifact_fact_is_current(cls, fact: dict) -> bool:
        target, properties = fact.get("target"), fact.get("properties")
        if not isinstance(target, Mapping) or not isinstance(properties, Mapping):
            return False
        observed = cls._artifact_fact(target.get("path"))
        return observed is not None and all(
            key in observed["properties"]
            and type(observed["properties"][key]) is type(value)
            and observed["properties"][key] == value
            for key, value in properties.items()
        )

    def set_execution_binding(self, binding: dict | None) -> None:
        """Persist the immutable Chat run/provider/model/reasoning identity."""

        source = binding if isinstance(binding, dict) else {}
        safe = {
            key: _bounded(source.get(key), maximum)
            for key, maximum in (
                ("conversation_id", 256),
                ("request_id", 256),
                ("turn_id", 256),
                ("run_id", 256),
                ("provider_id", 64),
                ("model_id", 160),
                ("effective_reasoning", 64),
            )
        }
        if any(not value for value in safe.values()) or safe["turn_id"] != self.turn_id:
            raise ValueError("execution binding is incomplete")
        with self._lock, self._process_lock():
            self._refresh()
            existing = self._state.get("execution_binding") or {}
            if existing and existing != safe:
                raise ValueError("turn execution binding is immutable")
            self._state["execution_binding"] = safe
            self._persist()

    def set_goal_contract(self, contract) -> None:
        """Persist one validated immutable semantic authority for this turn."""

        from osia.goal_contract import GoalContract, validate_goal_contract

        if not isinstance(contract, GoalContract):
            raise TypeError("goal contract is required")
        validated = validate_goal_contract(contract)
        if _bounded(validated.root_objective, 32_000) != self.objective:
            raise ValueError("goal contract objective does not match turn")
        safe = json.loads(json.dumps(
            validated.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ))
        encoded = json.dumps(
            safe, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 128 * 1024:
            raise ValueError("goal contract is too large")
        fingerprint = _bounded(validated.fingerprint, 128)
        if not fingerprint:
            raise ValueError("goal contract fingerprint is required")
        with self._lock, self._process_lock():
            self._refresh()
            execution_binding = self._state.get("execution_binding") or {}
            contract_binding = safe.get("execution_binding") or {}
            specified_contract_binding = {
                key: value
                for key, value in contract_binding.items()
                if value != "not_specified"
            }
            if execution_binding != specified_contract_binding:
                raise ValueError(
                    "goal contract execution binding does not match turn"
                )
            existing = self._state.get("goal_contract") or {}
            existing_fingerprint = str(
                self._state.get("goal_contract_fingerprint") or ""
            )
            if existing and (
                existing != safe or existing_fingerprint != fingerprint
            ):
                raise ValueError("turn goal contract is immutable")
            self._state["goal_contract"] = safe
            self._state["goal_contract_fingerprint"] = fingerprint
            self._persist()

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
        try:
            temporary.write_text(
                json.dumps(self._state, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            for delay in (0.02, 0.05, 0.10, 0.20, None):
                try:
                    os.replace(temporary, self._path)
                    return
                except PermissionError as exc:
                    # A Windows reader can briefly deny replacement of the
                    # same file. Retry the atomic write while its owner holds
                    # the existing turn locks.
                    if (
                        delay is None or sys.platform != "win32"
                        or getattr(exc, "winerror", None) not in (5, 32, 33)
                    ):
                        raise
                    time.sleep(delay)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

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
            if item.get("status") != "active":
                continue
            owner_pid = int(item.get("owner_pid") or 0)
            owner_identity = str(item.get("owner_process_identity") or "")
            owner_alive, live_identity = live_process_identity(owner_pid)
            if owner_alive and (
                not owner_identity
                or not live_identity
                or owner_identity == live_identity
            ):
                # Another OSIA process may legitimately own this invocation.
                # A different PID alone is never restart/death evidence.
                continue
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

    def prepare_durable_tool_resume(self, attempt_id: str) -> None:
        """Bind restart continuation to one exact active durable receipt.

        An interrupted provider invocation is not a second external effect when
        the exact durable ToolAttemptStore record exists.  The terminality
        projection may be missing after a crash; restore it from the durable
        identity without redispatching the attempt.  Remove only that redundant
        provider-process barrier; uncertainty about the action itself remains
        owned by the durable attempt and its bounded read-only reconciliation
        path.
        """

        key = _bounded(attempt_id, 192)
        if not key:
            raise ValueError("tool attempt id is required")
        from osia.tool_attempts import (
            RECOVERABLE_STATUSES,
            ToolAttemptStatus,
            get_tool_attempt_store,
        )

        with self._lock, self._process_lock():
            self._refresh()
            durable = get_tool_attempt_store().get_attempt(key)
            if (
                self.terminal_state
                or self._state.get("final_reservation")
                or not isinstance(durable, dict)
                or not self._tool_attempt_matches_projection(
                    key,
                    (self._state.get("active_attempts") or {}).get(key),
                    durable,
                )
                or ToolAttemptStatus(durable.get("status"))
                not in RECOVERABLE_STATUSES
            ):
                raise ValueError("durable tool attempt is not resumable")
            active = self._state.setdefault("active_attempts", {})
            prior = active.get(key)
            active[key] = {
                "status": str(
                    (prior or {}).get("status")
                    or ToolAttemptStatus(durable["status"]).value
                ),
                "updated_at": str((prior or {}).get("updated_at") or _now()),
                "identity": self._tool_attempt_identity(durable),
            }
            unknown = self._state.setdefault("unknown_effects", {})
            interrupted_ids = {
                str(item.get("invocation_id") or "")
                for item in self._state.get("invocations") or []
                if item.get("status") == "interrupted_outcome_unknown"
            }
            removable = {
                unknown_id
                for unknown_id, value in unknown.items()
                if unknown_id in interrupted_ids
                and isinstance(value, dict)
                and value.get("reason_code") == "provider_process_interrupted"
            }
            for unknown_id in removable:
                unknown.pop(unknown_id, None)
            for item in self._state.get("invocations") or []:
                if str(item.get("invocation_id") or "") in removable:
                    item["status"] = "interrupted_waiting_durable_attempt"
            self._persist()

    @staticmethod
    def _tool_attempt_identity(record: Mapping) -> dict:
        references = record.get("provider_references")
        references = references if isinstance(references, Mapping) else {}
        values = {
            "tool_attempt_id": record.get("tool_attempt_id"),
            "tool_call_id": record.get("tool_call_id"),
            "provider_native_call_id": references.get(
                "provider_native_call_id"
            ),
            "turn_id": record.get("turn_id"),
            "conversation_id": record.get("conversation_id"),
            "request_id": record.get("request_id"),
            "provider_id": record.get("provider_id"),
            "model_id": record.get("model_id"),
            "owner_scope": record.get("owner_scope"),
            "execution_epoch": record.get("execution_epoch"),
        }
        return {
            key: str(value or "")[:256]
            for key, value in values.items()
            if value not in (None, "")
        }

    def _tool_attempt_matches_projection(
        self,
        attempt_id: str,
        projection: object,
        record: Mapping,
    ) -> bool:
        """Validate a durable attempt against its exact turn projection."""

        if (
            str(record.get("tool_attempt_id") or "") != str(attempt_id)
            or str(record.get("turn_id") or "") != self.turn_id
        ):
            return False
        identity = (
            projection.get("identity")
            if isinstance(projection, Mapping) else {}
        )
        identity = identity if isinstance(identity, Mapping) else {}
        durable_identity = self._tool_attempt_identity(record)
        for field, expected in identity.items():
            if (
                expected not in (None, "")
                and str(durable_identity.get(field) or "")
                != str(expected)
            ):
                return False
        binding = self._state.get("execution_binding") or {}
        if not isinstance(binding, Mapping):
            binding = {}
        for field in (
            "conversation_id", "request_id", "provider_id", "model_id",
        ):
            expected = str(binding.get(field) or "")
            if expected and str(record.get(field) or "") != expected:
                return False
        return True

    @staticmethod
    def _durable_terminal_outcome(record: Mapping) -> str:
        """Project existing terminal ToolAttempt semantics without conversion."""

        from osia.action_truth import normalize_action_truth
        from osia.tool_attempts import ToolAttemptStatus

        terminal_outcome = str(record.get("terminal_outcome") or "")
        known_outcomes = {
            "success_verified", "success_unverified", "command_completed",
            "failed_before_effect", "confirmed_failure_before_effect",
            "rejected_before_execution", "failed_after_effect",
            "outcome_unknown", "unknown_after_start", "cancelled_proven",
        }
        if terminal_outcome in known_outcomes:
            return terminal_outcome
        try:
            status = ToolAttemptStatus(record.get("status"))
        except (TypeError, ValueError):
            return ""
        if status is ToolAttemptStatus.SUCCEEDED:
            truth = normalize_action_truth(
                record.get("action_truth"), legacy_record=record,
            )
            return (
                "success_verified"
                if truth.get("effect_class") == "read_only"
                or truth.get("effect_truth") == "verified"
                else "success_unverified"
            )
        return {
            ToolAttemptStatus.FAILED_BEFORE_EFFECT:
                "confirmed_failure_before_effect",
            ToolAttemptStatus.FAILED_AFTER_EFFECT: "failed_after_effect",
            ToolAttemptStatus.UNKNOWN_AFTER_START: "outcome_unknown",
            ToolAttemptStatus.CANCELLED_PROVEN: "cancelled_proven",
        }.get(status, "")

    def reconcile_tool_attempt(self, attempt_id: str) -> bool:
        """Project terminal facts from one exact durable ToolAttempt, if any."""

        key = _bounded(attempt_id, 192)
        if not key:
            return False
        try:
            from osia.tool_attempts import (
                TERMINAL_STATUSES,
                ToolAttemptStatus,
                get_tool_attempt_store,
            )

            durable = get_tool_attempt_store().get_attempt(key)
            if not isinstance(durable, dict):
                return False
            with self._lock:
                self._refresh()
                projection = (self._state.get("active_attempts") or {}).get(
                    key,
                )
                if not self._tool_attempt_matches_projection(
                    key, projection, durable,
                ):
                    return False
                status = ToolAttemptStatus(durable.get("status"))
                if status not in TERMINAL_STATUSES:
                    return False
                outcome = self._durable_terminal_outcome(durable)
                if not outcome:
                    return False
                references = durable.get("provider_references")
                references = (
                    references if isinstance(references, Mapping) else {}
                )
                action_truth = durable.get("action_truth")
                action_truth = (
                    action_truth if isinstance(action_truth, Mapping) else {}
                )
                evidence = {
                    **self._tool_attempt_identity(durable),
                    "terminal_status": status.value,
                    "terminal_outcome": outcome,
                    "dispatch_state": str(
                        durable.get("dispatch_state") or ""
                    )[:80],
                    "reason_code": str(
                        durable.get("reason_code") or ""
                    )[:160],
                    "action_id": str(action_truth.get("action_id") or "")[:256],
                    "semantic_effect_id": str(
                        action_truth.get("semantic_effect_id") or ""
                    )[:256],
                    "effect_truth": str(
                        action_truth.get("effect_truth") or ""
                    )[:80],
                    "provider_native_call_id": str(
                        references.get("provider_native_call_id") or ""
                    )[:256],
                }
                self.record_typed_evidence(
                    outcome=outcome,
                    evidence=evidence,
                    attempt_id=key,
                    reason_code=(
                        str(durable.get("reason_code") or "")
                        or "durable_tool_attempt_terminal"
                    ),
                )
                active = self._state.setdefault("active_attempts", {})
                active.pop(key, None)
                if outcome not in {
                    "outcome_unknown", "unknown_after_start",
                    "success_unverified",
                }:
                    self._state.setdefault("unknown_effects", {}).pop(key, None)
                    self._state.setdefault(
                        "current_state_refreshes", {},
                    ).pop(key, None)
                cancellation = self._state.get("cancellation") or {}
                if (
                    cancellation.get("requested")
                    and not active
                    and not self._state.get("unknown_effects")
                ):
                    cancellation["effect_possible"] = False
                if (
                    self._state.get("permission_state") == "pending"
                    and not active
                ):
                    self._state["permission_state"] = "resolved"
                self._persist()
                return True
        except Exception:
            # A missing, unreadable, or mismatched durable record is never
            # evidence that the attempt succeeded or became terminal.
            return False

    def _reconcile_active_tool_attempts(self) -> None:
        active = self._state.get("active_attempts") or {}
        for attempt_id in list(active):
            self.reconcile_tool_attempt(str(attempt_id))

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
                or (
                    cancellation.get("requested")
                    and not cancellation.get("effect_possible")
                )
            ):
                return ""
            required = list(self._state.get("required_postconditions") or [])
            target = _bounded(postcondition, 512) or (
                str((self._state.get("remaining_work") or required or [""])[0])
            )
            invocation_id = "inv_" + secrets.token_urlsafe(18)
            _owner_alive, owner_process_identity = live_process_identity(
                os.getpid()
            )
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
                "owner_process_identity": owner_process_identity,
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
        native_thread_id: str = "",
        native_status: str = "",
        response_fingerprint: str = "",
        native_tool_names: Iterable[str] = (),
        native_tool_schema_fingerprint: str = "",
        strategy_revision: int = 0,
        world_revision: int = 0,
    ) -> None:
        """Record an invocation boundary without treating it as turn success."""

        with self._lock:
            self._refresh()
            for item in reversed(self._state.setdefault("invocations", [])):
                if item.get("invocation_id") == invocation_id:
                    item["status"] = "provider_invocation_completed"
                    item["completed_at"] = _now()
                    item["native_turn_id"] = _bounded(native_turn_id, 192)
                    item["native_thread_id"] = _bounded(native_thread_id, 192)
                    item["native_status"] = _bounded(native_status, 80)
                    item["response_fingerprint"] = _bounded(response_fingerprint, 64)
                    item["native_tool_names"] = [
                        _bounded(name, 128)
                        for name in native_tool_names
                        if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", str(name or ""))
                    ][:64]
                    fingerprint = str(
                        native_tool_schema_fingerprint or ""
                    ).lower()
                    item["native_tool_schema_fingerprint"] = (
                        fingerprint
                        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) else ""
                    )
                    try:
                        item["strategy_revision"] = max(
                            0, int(strategy_revision or 0),
                        )
                        item["world_revision"] = max(
                            0, int(world_revision or 0),
                        )
                    except (TypeError, ValueError, OverflowError):
                        item["strategy_revision"] = 0
                        item["world_revision"] = 0
                    break
            self._persist()

    @staticmethod
    def _material_state_payload(snapshot: dict) -> dict:
        """Project only factual material execution state; omit prose/labels."""

        observation = snapshot.get("latest_desktop_observation") or {}
        observation = observation if isinstance(observation, dict) else {}
        mission = snapshot.get("mission") or {}
        nodes = mission.get("action_nodes") if isinstance(mission, dict) else []
        target_hints = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            target_hints.append({
                "application": _bounded(node.get("required_application"), 160),
                "target_hint": (
                    node.get("target_hint")
                    if isinstance(node.get("target_hint"), dict) else {}
                ),
            })
        return {
            "target_hints": target_hints[:8],
            "requested_target_candidates": (
                observation.get("requested_target_candidates") or []
            )[:8],
            "target_resolution": _bounded(
                observation.get("target_resolution"), 80,
            ),
            "target_conflict": observation.get("target_conflict") is True,
            "target_foreground": observation.get("target_foreground") is True,
            "target_minimized": observation.get("target_minimized") is True,
            "focus_observable": observation.get("focus_observable") is True,
            "focused_surface_ref": _bounded(
                observation.get("focused_surface_ref"), 160,
            ),
            "window_generation": _bounded(
                observation.get("window_generation"), 160,
            ),
            "structural_epoch": observation.get("structural_epoch") or 0,
            "capability": _bounded(
                observation.get("last_capability"), 160,
            ),
            "typed_result": _bounded(
                observation.get("last_typed_result"), 160,
            ),
            "requested_application": _bounded(
                observation.get("requested_application"), 160,
            ),
            "candidate_count": observation.get("target_candidate_count") or 0,
            "observed_surfaces": observation.get("top_level_surfaces") or [],
            "world_state_revision": snapshot.get("evidence_revision") or 0,
            "available_tools": sorted({
                _bounded(item, 128)
                for item in snapshot.get("available_tools") or []
                if _bounded(item, 128)
            }),
            "safe_strategy_frontier": sorted({
                _bounded(item, 160)
                for item in snapshot.get("alternatives_still_applicable") or []
                if _bounded(item, 160)
            }),
            "uia_actionable_candidate_count": len(
                observation.get("uia_actionable_frontier") or []
            ),
            "launch_capable_mechanisms": [
                _bounded(item.get("mechanism_id"), 160)
                for item in snapshot.get("mechanisms") or []
                if isinstance(item, dict)
                and (
                    item.get("attempted_effect") == "application_open"
                    or str(item.get("mechanism_id") or "").startswith(
                        "direct:open_application:"
                    )
                )
            ][:16],
        }

    def _record_first_causal_error(
        self,
        *,
        reason_code: str,
        evidence: dict,
    ) -> None:
        if not self._state.get("first_causal_error"):
            self._state["first_causal_error"] = {
                "reason_code": _bounded(reason_code, 160),
                "evidence": evidence,
                "recorded_at": _now(),
            }
        typed = self._state.setdefault("typed_evidence", [])
        entry = {
            "outcome": "structural_observation",
            "evidence_token": _fingerprint(evidence),
            "postcondition": "",
            "attempt_id": "",
            "reason_code": _bounded(reason_code, 160),
            "recorded_at": _now(),
        }
        if not any(
            item.get("reason_code") == entry["reason_code"]
            and item.get("evidence_token") == entry["evidence_token"]
            for item in typed if isinstance(item, dict)
        ):
            typed.append(entry)
            self._state["typed_evidence"] = typed if self._state.get("outcome_contract") else typed[-128:]

    def set_strategy_snapshot(self, snapshot: dict | None) -> None:
        with self._lock:
            self._refresh()
            if self._state.get("outcome_contract"):
                return  # Legacy strategy copies are not canonical facts.
            safe = dict(snapshot) if isinstance(snapshot, dict) else {}
            authoritative_proven = dict(
                self._state.get("proven_postconditions") or {}
            )
            required = tuple(
                self._state.get("required_postconditions") or ()
            )
            if authoritative_proven and safe:
                snapshot_proven = dict(safe.get("proven_postconditions") or {})
                snapshot_proven.update({
                    postcondition: authoritative_proven[postcondition]
                    for postcondition in required
                    if postcondition in authoritative_proven
                })
                safe["proven_postconditions"] = snapshot_proven
                evidence = list(safe.get("postcondition_evidence") or [])
                for value in snapshot_proven.values():
                    if value not in evidence:
                        evidence.append(value)
                safe["postcondition_evidence"] = evidence[-64:]
                mission = dict(safe.get("mission") or {})
                nodes = []
                completed_node_ids = set()
                for raw_node in mission.get("action_nodes") or []:
                    node = dict(raw_node) if isinstance(raw_node, dict) else raw_node
                    if isinstance(node, dict):
                        node_required = tuple(
                            node.get("required_postconditions") or ()
                        )
                        if node_required and all(
                            item in snapshot_proven for item in node_required
                        ):
                            node["status"] = "succeeded"
                            node_id = str(node.get("node_id") or "")
                            if node_id:
                                completed_node_ids.add(node_id)
                    nodes.append(node)
                if nodes:
                    mission["action_nodes"] = nodes
                    safe["mission"] = mission
                mechanism_nodes = {
                    str(item.get("mechanism_id") or ""): str(
                        item.get("node_id") or ""
                    )
                    for item in safe.get("mechanisms") or []
                    if isinstance(item, dict)
                }
                safe["alternatives_still_applicable"] = [
                    item
                    for item in safe.get("alternatives_still_applicable") or []
                    if mechanism_nodes.get(str(item or ""), "")
                    not in completed_node_ids
                ]
                goal = dict(safe.get("goal") or {})
                goal_required = tuple(goal.get("required_postconditions") or ())
                if goal_required and all(
                    item in snapshot_proven for item in goal_required
                ):
                    goal["status"] = "succeeded"
                    safe["goal"] = goal
                if required and all(
                    item in snapshot_proven for item in required
                ):
                    safe["succeeded"] = True
                    safe["alternatives_still_applicable"] = []
            self._state["strategy_snapshot"] = safe
            frontier = safe.get("alternatives_still_applicable") or []
            self._state["strategy_frontier"] = [
                _bounded(item, 160) for item in frontier if _bounded(item, 160)
            ][:64]
            if (
                self.execution_required
                and not safe.get("runtime_available_mechanisms")
                and not self._state["strategy_frontier"]
            ):
                self._state["strategy_frontier"] = ["provider_capability_selection"]
            material_state = self._material_state_payload(safe)
            binding = self._state.get("execution_binding") or {}
            material_state.update({
                "provider": _bounded(binding.get("provider_id"), 80),
                "model": _bounded(binding.get("model_id"), 160),
            })
            latest_provider_receipt = next((
                item for item in reversed(self._state.get("invocations") or [])
                if isinstance(item, dict)
                and item.get("status") == "provider_invocation_completed"
            ), {})
            material_state.update({
                "native_tool_names": list(
                    latest_provider_receipt.get("native_tool_names") or []
                ),
                "native_tool_schema_fingerprint": str(
                    latest_provider_receipt.get(
                        "native_tool_schema_fingerprint"
                    ) or ""
                ),
                "provider_strategy_revision": int(
                    latest_provider_receipt.get("strategy_revision") or 0
                ),
                "provider_world_revision": int(
                    latest_provider_receipt.get("world_revision") or 0
                ),
            })
            resolution = str(material_state.get("target_resolution") or "")
            if resolution in {"target_missing", "target_ambiguous"}:
                reason = (
                    "TARGET_CONFLICT"
                    if material_state.get("target_conflict") else
                    "TARGET_AMBIGUOUS"
                    if resolution == "target_ambiguous" else
                    "TARGET_MISSING"
                )
                v4_visual_frontier = bool(
                    "computer_screenshot"
                    in set(material_state.get("available_tools") or ())
                )
                launch_frontier = [
                    item for item in self._state["strategy_frontier"]
                    if item in material_state[
                        "launch_capable_mechanisms"
                    ]
                ]
                if not v4_visual_frontier:
                    self._state["strategy_frontier"] = launch_frontier
                self._state["structural_wait_reason"] = (
                    "" if launch_frontier or v4_visual_frontier else reason
                )
                if not v4_visual_frontier:
                    self._record_first_causal_error(
                        reason_code=reason,
                        evidence=material_state,
                    )
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
            self._bind_strategy_unknown_effects(safe)
            self._resolve_strategy_unknown_effects(safe)
            if self._state.get("outcome_contract"):
                self._state["strategy_frontier"] = []
                self._persist()
                return
            proven = safe.get("proven_postconditions") or {}
            completed = safe.get("completed_effects") or []
            required = set(self._state.get("required_postconditions") or [])
            completed_proofs: list[tuple[str, object]] = []
            for item in completed if isinstance(completed, list) else []:
                if not isinstance(item, dict):
                    continue
                attempted_effect = _bounded(item.get("attempted_effect"), 160)
                if attempted_effect not in required:
                    continue
                evidence = item.get("evidence") or {
                    "attempted_effect": attempted_effect,
                    "target_application": _bounded(
                        item.get("target_application"), 80,
                    ),
                    "target_surface": _bounded(item.get("target_surface"), 80),
                    "observed_at": item.get("observed_at") or 0.0,
                }
                completed_proofs.append((attempted_effect, evidence))
            if isinstance(proven, dict) and proven:
                # Preserve reconciliation before nested proof calls refresh
                # this controller from its durable file.
                self._persist()
                for postcondition, evidence in proven.items():
                    self.prove_postcondition(str(postcondition), str(evidence))
            else:
                self._persist()
            # A completed-effect fact is durable execution evidence, but it may
            # only discharge the exact terminality requirement it names.  It
            # must never widen or replace the turn's required work.
            for postcondition, evidence in completed_proofs:
                self.prove_postcondition(postcondition, evidence)

    @staticmethod
    def _strategy_node(snapshot: dict, node_id: str) -> dict:
        mission = snapshot.get("mission") or {}
        nodes = mission.get("action_nodes") if isinstance(mission, dict) else []
        for item in nodes or []:
            if isinstance(item, dict) and str(item.get("node_id") or "") == node_id:
                return item
        goal = snapshot.get("goal")
        if isinstance(goal, dict) and str(goal.get("node_id") or "") == node_id:
            return goal
        return {}

    @staticmethod
    def _strategy_attempt(snapshot: dict, attempt_id: str) -> dict:
        for item in snapshot.get("attempts") or []:
            if (
                isinstance(item, dict)
                and str(item.get("attempt_id") or "") == attempt_id
            ):
                return item
        return {}

    @staticmethod
    def _desktop_resolution_kind(event: dict) -> str:
        if (
            str(event.get("schema_version") or "") != "1"
            or str(event.get("evidence_kind") or "")
            != "exact_independent_desktop_state"
            or str(event.get("evidence_source") or "")
            != "inspect_desktop_surfaces"
            or not str(event.get("evidence") or "")
        ):
            return ""
        resolution = str(event.get("resolution") or "")
        outcome = str(event.get("evidence_outcome") or "")
        reason = str(event.get("reason_code") or "")
        if (
            resolution == "completed"
            and outcome == "confirmed_success"
            and reason in {
                "exact_final_state_observation",
                "exact_desktop_target_focus_observed",
            }
        ):
            return "completed"
        if (
            resolution == "primitive_completed"
            and outcome == "confirmed_success"
            and reason in {
                "exact_desktop_primitive_state_change",
                "exact_desktop_observable_effect_change",
            }
            and str(event.get("substep_id") or "")
        ):
            return "primitive_completed"
        if (
            resolution == "primitive_not_applied"
            and outcome == "confirmed_absence"
            and reason == "exact_desktop_primitive_absent_no_partial_effect"
            and str(event.get("substep_id") or "")
            and str(event.get("no_partial_effect") or "").lower() == "true"
        ):
            return "primitive_not_applied"
        if (
            resolution == "absent"
            and outcome == "confirmed_absence"
            and reason == "exact_postcondition_absent_no_partial_effect"
            and str(event.get("no_partial_effect") or "").lower() == "true"
        ):
            return "not_applied"
        return ""

    def _strategy_effect_identities(self, snapshot: dict) -> list[dict]:
        identities = []
        frozen_attempt_id = str(snapshot.get("frozen_attempt_id") or "")
        frozen_node_id = str(snapshot.get("frozen_node_id") or "")
        frozen_mechanism_id = str(snapshot.get("frozen_mechanism_id") or "")
        candidates = []
        if frozen_attempt_id and frozen_node_id and frozen_mechanism_id:
            candidates.append({
                "attempt_id": frozen_attempt_id,
                "node_id": frozen_node_id,
                "mechanism_id": frozen_mechanism_id,
            })
        for event in snapshot.get("reconciliation_events") or []:
            if isinstance(event, dict) and self._desktop_resolution_kind(event):
                candidates.append(event)
        seen = set()
        for candidate in candidates:
            attempt_id = str(candidate.get("attempt_id") or "")
            node_id = str(candidate.get("node_id") or "")
            mechanism_id = str(candidate.get("mechanism_id") or "")
            identity_key = (attempt_id, node_id, mechanism_id)
            if not all(identity_key) or identity_key in seen:
                continue
            attempt = self._strategy_attempt(snapshot, attempt_id)
            node = self._strategy_node(snapshot, node_id)
            if (
                not attempt
                or not node
                or str(attempt.get("node_id") or "") != node_id
                or str(attempt.get("mechanism_id") or "") != mechanism_id
                or str(attempt.get("status") or "") not in {
                    "selected", "ambiguous_effect", "progressed", "succeeded",
                }
            ):
                continue
            primitive_postcondition = _bounded(
                attempt.get("primitive_postcondition"), 512,
            )
            attempted_effect = _bounded(
                attempt.get("attempted_effect"), 512,
            )
            postconditions = (
                (attempted_effect,)
                if attempted_effect else
                (primitive_postcondition,)
                if primitive_postcondition else
                tuple(
                    _bounded(item, 512)
                    for item in node.get("required_postconditions") or ()
                    if _bounded(item, 512)
                )
            )
            if not postconditions:
                postcondition = _bounded(
                    node.get("observable_success_postcondition"), 512,
                )
                postconditions = (postcondition,) if postcondition else ()
            if not postconditions:
                continue
            identities.append({
                "strategy_attempt_id": attempt_id,
                "semantic_node_id": node_id,
                "mechanism_id": mechanism_id,
                "postconditions": postconditions,
                "target_application": _bounded(
                    attempt.get("target_application")
                    or node.get("required_application"), 160,
                ),
                "target_surface": _bounded(
                    attempt.get("target_surface")
                    or node.get("required_surface"), 160,
                ),
                "strategy_dispatched_at": _as_float(
                    attempt.get("dispatched_at")
                    or candidate.get("dispatched_at")
                ),
                "strategy_dispatch_evidence_revision": int(
                    _as_float(
                        attempt.get("dispatch_evidence_revision")
                        or candidate.get("dispatch_evidence_revision")
                    )
                ),
                "composite_id": _bounded(attempt.get("composite_id"), 160),
                "substep_id": _bounded(attempt.get("substep_id"), 160),
                "substep_index": int(
                    _as_float(attempt.get("substep_index"))
                ),
            })
            seen.add(identity_key)
        return identities

    def _bind_strategy_unknown_effects(self, snapshot: dict) -> None:
        unknown = self._state.setdefault("unknown_effects", {})
        identities = self._strategy_effect_identities(snapshot)
        possible: dict[str, list[dict]] = {}
        for durable_attempt_id, item in unknown.items():
            if not isinstance(item, dict) or item.get("semantic_node_id"):
                continue
            if (
                not durable_attempt_id
                or str(item.get("attempt_id") or "") != durable_attempt_id
            ):
                continue
            postcondition = _bounded(item.get("postcondition"), 512)
            matches = [
                identity for identity in identities
                if postcondition in identity["postconditions"]
            ]
            if len(matches) == 1:
                possible[durable_attempt_id] = matches
        identity_counts: dict[tuple[str, str, str], int] = {}
        for matches in possible.values():
            identity = matches[0]
            key = (
                identity["strategy_attempt_id"],
                identity["semantic_node_id"],
                identity["mechanism_id"],
            )
            identity_counts[key] = identity_counts.get(key, 0) + 1
        for durable_attempt_id, matches in possible.items():
            identity = matches[0]
            key = (
                identity["strategy_attempt_id"],
                identity["semantic_node_id"],
                identity["mechanism_id"],
            )
            if identity_counts.get(key) != 1:
                continue
            item = unknown[durable_attempt_id]
            item.update({
                "semantic_node_id": identity["semantic_node_id"],
                "strategy_attempt_id": identity["strategy_attempt_id"],
                "mechanism_id": identity["mechanism_id"],
                "target_application": identity["target_application"],
                "target_surface": identity["target_surface"],
                "strategy_dispatched_at": identity["strategy_dispatched_at"],
                "strategy_dispatch_evidence_revision": identity[
                    "strategy_dispatch_evidence_revision"
                ],
                "composite_id": identity["composite_id"],
                "substep_id": identity["substep_id"],
                "substep_index": identity["substep_index"],
                "strategy_bound_at": _now(),
            })

    def _resolve_strategy_unknown_effects(self, snapshot: dict) -> None:
        unknown = self._state.setdefault("unknown_effects", {})
        resolutions = self._state.setdefault("unknown_effect_resolutions", [])
        resolved_strategy_attempts = {
            str(item.get("strategy_attempt_id") or "")
            for item in resolutions
            if isinstance(item, dict)
        }
        proven = snapshot.get("proven_postconditions") or {}
        proven = proven if isinstance(proven, dict) else {}
        for event in snapshot.get("reconciliation_events") or []:
            if not isinstance(event, dict):
                continue
            resolution = self._desktop_resolution_kind(event)
            strategy_attempt_id = str(event.get("attempt_id") or "")
            if not resolution or strategy_attempt_id in resolved_strategy_attempts:
                continue
            node_id = str(event.get("node_id") or "")
            mechanism_id = str(event.get("mechanism_id") or "")
            postcondition = _bounded(event.get("postcondition"), 512)
            node = self._strategy_node(snapshot, node_id)
            attempt = self._strategy_attempt(snapshot, strategy_attempt_id)
            primitive_resolution = resolution in {
                "primitive_completed", "primitive_not_applied",
            }
            required_postconditions = tuple(
                _bounded(item, 512)
                for item in node.get("required_postconditions") or ()
            ) if node else ()
            attempted_effect = _bounded(
                attempt.get("attempted_effect"), 512,
            ) if attempt else ""
            if (
                not node
                or not attempt
                or (
                    primitive_resolution
                    and (
                        postcondition
                        != _bounded(attempt.get("primitive_postcondition"), 512)
                        or str(event.get("composite_id") or "")
                        != str(attempt.get("composite_id") or "")
                        or str(event.get("substep_id") or "")
                        != str(attempt.get("substep_id") or "")
                    )
                )
                or (
                    not primitive_resolution
                    and postcondition != attempted_effect
                    and postcondition not in required_postconditions
                )
            ):
                continue
            candidates = [
                (durable_attempt_id, item)
                for durable_attempt_id, item in unknown.items()
                if isinstance(item, dict)
                and str(item.get("strategy_attempt_id") or "")
                == strategy_attempt_id
                and str(item.get("semantic_node_id") or "") == node_id
                and str(item.get("mechanism_id") or "") == mechanism_id
                and _bounded(item.get("postcondition"), 512) == postcondition
                and (
                    not primitive_resolution
                    or str(item.get("substep_id") or "")
                    == str(event.get("substep_id") or "")
                )
            ]
            if len(candidates) != 1:
                continue
            durable_attempt_id, original = candidates[0]
            target_application = _bounded(
                original.get("target_application"), 160,
            )
            target_surface = _bounded(original.get("target_surface"), 160)
            if (
                _bounded(event.get("target_application"), 160)
                != target_application
                or _bounded(event.get("target_surface"), 160) != target_surface
                or not _bounded(event.get("observed_surface"), 160)
                or (
                    target_application
                    and _application_identity(
                        event.get("observed_application"),
                    ) != _application_identity(target_application)
                )
            ):
                continue
            dispatched_at = _as_float(original.get("strategy_dispatched_at"))
            observed_at = _as_float(event.get("observed_at"))
            recorded_at = _recorded_epoch(original.get("recorded_at"))
            dispatch_revision = int(_as_float(
                original.get("strategy_dispatch_evidence_revision"),
            ))
            evidence_revision = int(_as_float(event.get("evidence_revision")))
            if (
                dispatched_at <= 0.0
                or _as_float(event.get("dispatched_at")) != dispatched_at
                or int(_as_float(
                    event.get("dispatch_evidence_revision"),
                )) != dispatch_revision
                or observed_at < max(dispatched_at, recorded_at)
                or evidence_revision <= dispatch_revision
            ):
                continue
            if (
                resolution == "completed"
                and postcondition != attempted_effect
                and postcondition not in proven
            ) or (
                resolution == "not_applied"
                and postcondition in proven
            ):
                continue
            evidence_token = _fingerprint(event)
            resolution_record = {
                "resolution_id": "resolution_" + evidence_token,
                "durable_attempt_id": durable_attempt_id,
                "strategy_attempt_id": strategy_attempt_id,
                "semantic_node_id": node_id,
                "mechanism_id": mechanism_id,
                "composite_id": str(event.get("composite_id") or ""),
                "substep_id": str(event.get("substep_id") or ""),
                "substep_index": int(_as_float(event.get("substep_index"))),
                "postcondition": postcondition,
                "target_application": target_application,
                "target_surface": target_surface,
                "resolution": resolution,
                "reason_code": str(event.get("reason_code") or ""),
                "evidence_token": evidence_token,
                "observed_at": observed_at,
                "resolved_at": _now(),
                "original_unknown": dict(original),
            }
            resolutions.append(resolution_record)
            self._state["unknown_effect_resolutions"] = resolutions[-128:]
            typed = self._state.setdefault("typed_evidence", [])
            typed.append({
                "outcome": (
                    "success_verified"
                    if resolution in {"completed", "primitive_completed"} else
                    "unknown_effect_reconciled_not_applied"
                ),
                "evidence_token": evidence_token,
                "postcondition": postcondition,
                "attempt_id": durable_attempt_id,
                "reason_code": str(event.get("reason_code") or ""),
                "recorded_at": _now(),
                "semantic_node_id": node_id,
                "strategy_attempt_id": strategy_attempt_id,
                "composite_id": str(event.get("composite_id") or ""),
                "substep_id": str(event.get("substep_id") or ""),
            })
            self._state["typed_evidence"] = typed if self._state.get("outcome_contract") else typed[-128:]
            tokens = self._state.setdefault("evidence_tokens", [])
            if evidence_token not in tokens:
                tokens.append(evidence_token)
                self._state["evidence_tokens"] = tokens[-128:]
                self._state["evidence_revision"] = self.evidence_revision + 1
            unknown.pop(durable_attempt_id, None)
            self._state.setdefault("active_attempts", {}).pop(
                durable_attempt_id, None,
            )
            cancellation = self._state.get("cancellation") or {}
            if cancellation.get("requested") and not unknown:
                cancellation["effect_possible"] = False
            resolved_strategy_attempts.add(strategy_attempt_id)

    def set_strategy_frontier(self, strategy_ids: Iterable[str]) -> None:
        with self._lock:
            self._refresh()
            self._state["strategy_frontier"] = [
                _bounded(item, 160) for item in strategy_ids if _bounded(item, 160)
            ][:64]
            self._persist()

    def mark_structural_wait(
        self,
        reason_code: str,
        *,
        evidence: object = "",
    ) -> None:
        """Record a factual structural wait without claiming a pending tool.

        WOR-303: this marker carries no terminal authority.  The safe frontier
        is preserved so the same canonical turn can continue or replan; the
        frontier must never be destroyed by a temporary structural wait.
        """

        reason = _bounded(reason_code, 160) or "WAIT_STRUCTURAL"
        safe_evidence = (
            evidence if isinstance(evidence, dict)
            else {"evidence_token": _fingerprint(evidence)}
        )
        with self._lock:
            self._refresh()
            self._state["structural_wait_reason"] = reason
            self._record_first_causal_error(
                reason_code=reason,
                evidence=safe_evidence,
            )
            self._persist()

    def record_typed_evidence(
        self,
        *,
        outcome: str,
        evidence: object = "",
        postcondition: str = "",
        attempt_id: str = "",
        reason_code: str = "",
        facts=None,
        effectful: bool = False,
    ) -> None:
        normalized = _bounded(outcome, 80).lower()
        evidence_token = _fingerprint(evidence) if evidence not in (None, "") else ""
        supplied_facts = [dict(facts)] if isinstance(facts, Mapping) else facts
        try:
            safe_facts = json.loads(json.dumps(supplied_facts or [], allow_nan=False))
        except (TypeError, ValueError):
            safe_facts = []
        if not isinstance(safe_facts, list):
            safe_facts = []
        from osia.outcome_contract import normalize_fact_path
        for fact in safe_facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("target"), dict):
                continue
            key = "path" if fact.get("kind") == "artifact" else "cwd" if fact.get("kind") == "process" else ""
            if key and key in fact["target"]:
                try:
                    fact["target"][key] = normalize_fact_path(fact["target"][key])
                except (ValueError, OSError):
                    fact.clear()
        entry = {
            "outcome": normalized,
            "evidence_token": evidence_token,
            "postcondition": _bounded(postcondition, 512),
            "attempt_id": _bounded(attempt_id, 192),
            "reason_code": _bounded(reason_code, 160),
            "recorded_at": _now(),
            "facts": safe_facts,
            "effectful": bool(effectful),
        }
        with self._lock:
            self._refresh()
            active = self._state.get("active_attempts") or {}
            if (
                entry["attempt_id"] in active
                and not self._terminal_evidence_identity_matches(
                    entry["attempt_id"], evidence,
                )
            ):
                return
            evidence_items = self._state.setdefault("typed_evidence", [])
            identity = {key: entry[key] for key in (
                "outcome", "evidence_token", "postcondition", "attempt_id", "reason_code", "facts", "effectful",
            )}
            duplicate = any(
                all(item.get(key) == value for key, value in identity.items())
                for item in evidence_items
            )
            first_failure_recorded = False
            if (
                normalized in {
                    "failed_before_effect",
                    "confirmed_failure_before_effect",
                    "rejected_before_execution",
                }
                and entry["reason_code"]
                and not self._state.get("first_causal_error")
            ):
                self._state["first_causal_error"] = {
                    "reason_code": entry["reason_code"],
                    "evidence": {
                        "outcome": normalized,
                        "evidence_token": evidence_token,
                        "postcondition": entry["postcondition"],
                        "attempt_id": entry["attempt_id"],
                    },
                    "recorded_at": _now(),
                }
                first_failure_recorded = True
            if duplicate:
                if first_failure_recorded:
                    self._persist()
                return
            if effectful:
                self._state["external_dispatch_observed"] = True
                if self._state.get("outcome_contract") and not self._state.get("final_reservation"):
                    self._state["proven_postconditions"] = {}
                    self._state["remaining_work"] = ["original_objective_satisfied"]
                    self._state["completion_text"] = ""
            evidence_items.append(entry)
            # A fresh observation of the same path supersedes older file
            # versions, even when an older receipt reported success.
            artifact_targets = {
                item.get("target", {}).get("path"): item for item in entry["facts"]
                if isinstance(item, dict) and item.get("kind") == "artifact"
            }
            if artifact_targets:
                for prior in evidence_items[:-1]:
                    if any(
                        isinstance(fact, dict) and fact.get("kind") == "artifact"
                        and fact.get("target", {}).get("path") in artifact_targets
                        and fact != artifact_targets[fact["target"]["path"]]
                        for fact in prior.get("facts") or []
                    ):
                        prior["superseded"] = True
                        if self._state.get("outcome_contract") and not self._state.get("final_reservation"):
                            self._state["proven_postconditions"] = {}
                            self._state["remaining_work"] = ["original_objective_satisfied"]
                            self._state["completion_text"] = ""
            self._state["typed_evidence"] = evidence_items if self._state.get("outcome_contract") else evidence_items[-128:]
            evidence_tokens = self._state.setdefault("evidence_tokens", [])
            if evidence_token and evidence_token not in evidence_tokens:
                evidence_tokens.append(evidence_token)
                self._state["evidence_tokens"] = evidence_tokens[-128:]
                self._state["evidence_revision"] = self.evidence_revision + 1
            if normalized in {"outcome_unknown", "unknown_after_start"}:
                key = entry["attempt_id"] or evidence_token or "unknown_effect"
                pending_refresh = (
                    self._state.setdefault(
                        "current_state_refreshes", {},
                    ).get(key)
                )
                if self._valid_current_state_refresh(
                    key, pending_refresh, turn_id=self.turn_id,
                ):
                    entry["current_state_refresh"] = dict(pending_refresh)
                self._state.setdefault("unknown_effects", {})[key] = entry
            elif normalized == "success_unverified":
                key = entry["attempt_id"] or evidence_token or "unverified_effect"
                self._state.setdefault("unknown_effects", {})[key] = entry
            elif normalized in {
                "success_verified",
                "failed_before_effect",
                "confirmed_failure_before_effect",
                "rejected_before_execution",
                "failed_after_effect",
                "cancelled_proven",
            }:
                key = entry["attempt_id"]
                if key:
                    self._state.setdefault("unknown_effects", {}).pop(key, None)
                    self._state.setdefault(
                        "current_state_refreshes", {},
                    ).pop(key, None)
            if (
                normalized in {
                    "success_verified",
                    "success_unverified",
                    "failed_before_effect",
                    "confirmed_failure_before_effect",
                    "rejected_before_execution",
                    "failed_after_effect",
                    "cancelled_proven",
                }
                and self._state.get("permission_state") == "pending"
                and not self._state.get("active_attempts")
            ):
                self._state["permission_state"] = "resolved"
            if normalized == "hard_safety_block" and entry["reason_code"] not in {
                "block_system_destruction", "system_destruction_absolute",
            }:
                self._state["hard_safety_reason"] = entry["reason_code"] or normalized
            self._persist()

    def _terminal_evidence_identity_matches(
        self,
        attempt_id: str,
        evidence: object,
    ) -> bool:
        active = self._state.get("active_attempts") or {}
        projection = active.get(attempt_id)
        identity = (
            projection.get("identity")
            if isinstance(projection, Mapping) else {}
        )
        if not isinstance(identity, Mapping) or not identity:
            return True
        payload = evidence if isinstance(evidence, Mapping) else {}
        supplied_identity = payload.get("attempt_identity")
        supplied_identity = (
            supplied_identity if isinstance(supplied_identity, Mapping) else {}
        )
        combined = {**payload, **supplied_identity}
        aliases = {
            "tool_attempt_id": ("tool_attempt_id",),
            "tool_call_id": (
                "tool_call_id", "provider_native_call_id", "call_id", "item_id",
            ),
            "provider_native_call_id": (
                "provider_native_call_id", "tool_call_id", "call_id", "item_id",
            ),
            "turn_id": ("turn_id",),
            "conversation_id": ("conversation_id",),
            "request_id": ("request_id", "provider_request_id"),
            "provider_id": ("provider_id",),
            "model_id": ("model_id",),
            "owner_scope": ("owner_scope",),
            "execution_epoch": ("execution_epoch",),
        }
        for field, expected in identity.items():
            if expected in (None, ""):
                continue
            supplied = next((
                combined.get(alias)
                for alias in aliases.get(field, (field,))
                if combined.get(alias) not in (None, "")
            ), None)
            if supplied is not None and str(supplied) != str(expected):
                return False
        return True

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
        reconcile_terminal = False
        with self._lock:
            self._refresh()
            active = self._state.setdefault("active_attempts", {})
            if normalized in {"pending", "running", "accepted", "waiting_verification", "active"}:
                projection = {"status": normalized, "updated_at": _now()}
                try:
                    from osia.tool_attempts import get_tool_attempt_store

                    durable = get_tool_attempt_store().get_attempt(key)
                    if (
                        isinstance(durable, dict)
                        and self._tool_attempt_matches_projection(
                            key, None, durable,
                        )
                    ):
                        projection["identity"] = self._tool_attempt_identity(
                            durable,
                        )
                except Exception:
                    pass
                active[key] = projection
            else:
                # A derived projection leaves the pending set only after the
                # exact durable ToolAttempt proves a matching terminal state.
                reconcile_terminal = True
            if permission_state:
                self._state["permission_state"] = _bounded(permission_state, 80)
            self._persist()
        if reconcile_terminal:
            self.reconcile_tool_attempt(key)

    def set_permission_state(self, state: str) -> None:
        """Record legacy permission diagnostics only; it carries no authority.

        WOR-301 removed the OSIA permission system.  The field is retained as
        an inert diagnostic and can never block, wait or terminalize a turn.
        """

        with self._lock:
            self._refresh()
            self._state["permission_state"] = _bounded(state, 80) or "none"
            self._persist()

    def mark_provider_fact_blocker(self, reason_code: str) -> None:
        """Record one proven non-retryable provider fact.

        WOR-303: the code comes directly from the escaped provider error
        (credentials, configuration, model access, hard billing/quota, proven
        contract incompatibility).  It is a single fact, never a failure
        count, and it is the only provider condition that may reserve a
        blocked final.
        """

        code = _bounded(reason_code, 160)
        if code not in _PROVEN_PROVIDER_FACT_BLOCKERS:
            return
        with self._lock:
            self._refresh()
            self._state["provider_fact_blocker"] = code
            self._persist()

    def require_execution(self, postcondition: str = "") -> None:
        """Promote a turn when a structured execution transition is observed."""

        with self._lock:
            self._refresh()
            if self._state.get("outcome_contract") or self.execution_required:
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
        arguments: dict | None = None,
    ) -> None:
        """Project one typed ToolExecutionRecord into the turn ledger."""

        outcome_value = getattr(getattr(execution_record, "outcome", None), "value", None)
        outcome = str(outcome_value or getattr(execution_record, "outcome", "") or "")
        reason_code = str(getattr(execution_record, "reason_code", "") or "")
        attempt_id = str(getattr(execution_record, "tool_attempt_id", "") or "")
        result = getattr(execution_record, "result", "")
        contract = getattr(execution_record, "contract", None)
        side_effect = str(
            getattr(
                getattr(contract, "side_effect", None),
                "value",
                getattr(contract, "side_effect", ""),
            )
            or ""
        )
        from osia.tool_contracts import ToolExecutionRecord, normalize_process_facts
        facts = []
        real_record = isinstance(execution_record, ToolExecutionRecord)
        completed = bool(
            real_record and execution_record.dispatched
            and not execution_record.timed_out and not execution_record.worker_may_be_active
            and getattr(getattr(contract, "spec", None), "name", "") == tool_name
        )
        process = normalize_process_facts(getattr(execution_record, "process_facts", None)) if completed else None
        if tool_name in {"bash", "run_command"} and process is not None:
            properties = {key: process[key] for key in ("exit_code", "output_truncated")}
            if process["combined_output"] is not None:
                properties["output"] = process["combined_output"]
            else:
                properties.update(stdout=process["stdout"], stderr=process["stderr"])
                properties["output"] = "\n".join(value for value in (process["stdout"], process["stderr"]) if value)
            facts.append({"kind": "process", "target": {"command": process["command"], "cwd": process["cwd"]},
                          "properties": properties})
            # A nonzero compiler exit is a completed process observation, not
            # proof of a successful build or a global objective blocker.
            outcome = "command_completed"
            if attempt_id:
                self.set_attempt_state(attempt_id, status="succeeded")
        elif (
            completed and not execution_record.is_error
            and outcome in {"success_verified", "success_unverified"}
            and tool_name in {"write_file", "edit_file_lines", "read_file", "get_file_info", "create_directory", "list_files", "delete_file", "delete_directory"}
        ):
            artifact = self._artifact_fact((arguments or {}).get("path"))
            if artifact is not None:
                facts.append(artifact)
        if tool_name == "declare_outcomes":
            # Internal declaration persistence is never an external fact.
            outcome = "metadata_recorded" if not getattr(execution_record, "is_error", False) else outcome
            facts = []
        if outcome == "success_unverified" and side_effect == "read_only":
            outcome = "success_verified"
            reason_code = reason_code or "read_only_result_returned"
        elif outcome == "success_unverified" and self._state.get("outcome_contract"):
            from osia.tool_contracts import ToolExecutionRecord

            if (
                isinstance(execution_record, ToolExecutionRecord)
                and tool_name in {"bash", "run_command"}
                and getattr(getattr(contract, "spec", None), "name", "") == tool_name
                and execution_record.dispatched
                and not execution_record.is_error
                and not execution_record.timed_out
                and not execution_record.worker_may_be_active
            ):
                # SUCCESS_UNVERIFIED + dispatched is persisted as
                # effect_confirmed by the dispatcher: the shell returned its
                # stdout/stderr, but no requested external outcome is proven.
                # Keep this observation separate from genuinely unknown
                # dispatches. It records only process facts; neither ActionTruth nor this
                # projection vetoes a fresh model-requested operation.
                outcome = "command_completed"
                reason_code = reason_code or "completed_shell_result_returned"
                if attempt_id:
                    self.set_attempt_state(attempt_id, status="succeeded")
        self.require_execution(postcondition)
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
            facts=facts,
            effectful=bool(getattr(execution_record, "dispatched", False) and side_effect != "read_only"),
        )
        if attempt_id:
            self.reconcile_tool_attempt(attempt_id)

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
            reason_code="unknown_effect_reconciled",
        )

    @staticmethod
    def _valid_current_state_refresh(
        attempt_id: str,
        receipt: object,
        *,
        turn_id: str,
    ) -> bool:
        if not isinstance(receipt, dict):
            return False
        try:
            captured_at = float(receipt.get("captured_at") or 0.0)
            expires_at = float(receipt.get("expires_at") or 0.0)
        except (TypeError, ValueError, OverflowError):
            return False
        return bool(
            receipt.get("purpose") == "current_state_refreshed_after_effect"
            and receipt.get("previous_tool_attempt_id") == attempt_id
            and receipt.get("turn_id") == turn_id
            and receipt.get("captured_after_dispatch") is True
            and receipt.get("image_consumed_by_same_model") is True
            and receipt.get("effect_verified") is False
            and receipt.get("goal_satisfied") is False
            and receipt.get("frame_id")
            and receipt.get("v4_loop_id")
            and receipt.get("native_window_id")
            and receipt.get("window_generation")
            and captured_at > 0.0
            and expires_at >= datetime.now(timezone.utc).timestamp()
        )

    def _unknown_effects_requiring_reconciliation(self) -> dict[str, dict]:
        unknown = self._state.get("unknown_effects") or {}
        pending_refreshes = self._state.get("current_state_refreshes") or {}
        return {
            str(attempt_id): item
            for attempt_id, item in unknown.items()
            if isinstance(item, dict)
            and not self._valid_current_state_refresh(
                str(attempt_id),
                item.get("current_state_refresh")
                or pending_refreshes.get(str(attempt_id)),
                turn_id=self.turn_id,
            )
        }

    def record_current_state_refresh(
        self,
        attempt_id: str,
        receipt: Mapping[str, object],
    ) -> bool:
        """Record current-state freshness without resolving an action effect."""

        key = _bounded(attempt_id, 192)
        safe = dict(receipt) if isinstance(receipt, Mapping) else {}
        with self._lock:
            self._refresh()
            unknown = self._state.setdefault("unknown_effects", {})
            item = unknown.get(key)
            active = self._state.get("active_attempts") or {}
            if (
                not isinstance(item, dict)
                and key not in active
            ) or not self._valid_current_state_refresh(
                key, safe, turn_id=self.turn_id,
            ):
                return False
            pending_refreshes = self._state.setdefault(
                "current_state_refreshes", {},
            )
            pending_refreshes[key] = dict(safe)
            refreshed_action_ids = set(
                str(value)
                for value in safe.get("refreshed_action_ids") or []
                if str(value)
            )
            for unknown_id, unknown_item in unknown.items():
                if not isinstance(unknown_item, dict):
                    continue
                previous = unknown_item.get("current_state_refresh")
                previous_action_id = (
                    str(previous.get("previous_action_id") or "")
                    if isinstance(previous, dict) else ""
                )
                if unknown_id == key or previous_action_id in refreshed_action_ids:
                    bound = dict(safe)
                    bound["previous_tool_attempt_id"] = str(unknown_id)
                    unknown_item["current_state_refresh"] = bound
            self._persist()
            return True

    def prove_postcondition(self, postcondition: str, evidence: object) -> bool:
        target = _bounded(postcondition, 512)
        if evidence in (None, ""):
            return False
        token = _fingerprint(evidence)
        with self._lock:
            self._refresh()
            if self._state.get("outcome_contract"):
                return False
            required = list(self._state.get("required_postconditions") or [])
            if not target and len(required) == 1:
                target = str(required[0])
            if target not in required:
                return False
            proven = self._state.setdefault("proven_postconditions", {})
            if target not in proven:
                proven[target] = token
                self._state["evidence_revision"] = self.evidence_revision + 1
            unknown_effects = self._state.setdefault("unknown_effects", {})
            reconciled_attempts = {
                attempt_id
                for attempt_id, item in unknown_effects.items()
                if (
                    not isinstance(item, dict)
                    or not item.get("semantic_node_id")
                ) and (
                    len(required) == 1
                    or str((item or {}).get("postcondition") or "") == target
                )
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

    def try_request_cancellation(
        self,
        *,
        effect_possible: bool,
        reason_code: str = "",
        superseded_by_turn_id: str = "",
    ) -> bool:
        """Atomically install the authoritative no-answer cancellation tombstone."""

        with self._lock, self._process_lock():
            self._refresh()
            cancellation = self._state.setdefault("cancellation", {})
            if (
                self._state.get("terminal_state")
                == TerminalState.USER_CANCELLED.value
                and cancellation.get("requested")
                and isinstance(self._state.get("final_reservation"), dict)
                and self._state.get("final_reservation")
            ):
                return True
            if (
                self._state.get("terminal_state")
                or self._state.get("final_reservation")
            ):
                return False
            possible = bool(
                effect_possible
                or cancellation.get("effect_possible")
                or self._state.get("active_attempts")
                or self._state.get("unknown_effects")
            )
            cancellation.update({
                "requested": True,
                "effect_possible": possible,
                "requested_at": _now(),
            })
            cancellation_reason = _bounded(reason_code, 160)
            superseded_by = _bounded(superseded_by_turn_id, 256)
            if cancellation_reason:
                cancellation["reason_code"] = cancellation_reason
            if superseded_by:
                cancellation["superseded_by_turn_id"] = superseded_by
            if possible and not self._state.get("unknown_effects"):
                self._state.setdefault("unknown_effects", {})[
                    "cancellation_possible_effect"
                ] = {
                    "outcome": "outcome_unknown",
                    "reason_code": "cancelled_effect_requires_reconciliation",
                    "recorded_at": _now(),
                }
            # Stop is the terminal execution authority even when a prior effect
            # still requires evidence.  The empty reservation is deliberate:
            # cancellation never fabricates an assistant answer, while late
            # mutation/observer evidence can continue to enrich the durable
            # receipt without reopening execution or final generation.
            self._state["terminal_state"] = TerminalState.USER_CANCELLED.value
            self._state["terminal_reason"] = "authoritative_user_cancellation"
            self._state["final_reservation"] = {
                "reservation_id": "final_" + secrets.token_urlsafe(18),
                "reserved_at": _now(),
                "terminal_state": TerminalState.USER_CANCELLED.value,
                "final_text": "",
            }
            self._persist()
            return True

    def request_cancellation(self, *, effect_possible: bool) -> None:
        self.try_request_cancellation(effect_possible=effect_possible)

    def refine_cancellation_proven_before_effect(self) -> bool:
        """Remove only the synthetic Stop uncertainty after exact proof."""

        with self._lock, self._process_lock():
            self._refresh()
            if (
                self._state.get("terminal_state")
                not in {"", TerminalState.USER_CANCELLED.value, None}
            ):
                return False
            cancellation = self._state.get("cancellation") or {}
            if not cancellation.get("requested"):
                return False
            unknown = self._state.setdefault("unknown_effects", {})
            synthetic = unknown.get("cancellation_possible_effect")
            if isinstance(synthetic, dict) and synthetic.get(
                "reason_code"
            ) == "cancelled_effect_requires_reconciliation":
                unknown.pop("cancellation_possible_effect", None)
            if not unknown:
                cancellation["effect_possible"] = False
            self._persist()
            return True

    def mark_hard_safety_block(self, reason_code: str) -> None:
        with self._lock, self._process_lock():
            self._refresh()
            self._state["hard_safety_reason"] = _bounded(reason_code, 160) or "absolute_safety_barrier"
            self._persist()

    def _next_strategy(self, provider_route: str) -> tuple[str, str]:
        """Return the first factually available strategy for remaining work.

        WOR-303: there is no per-strategy guidance quota.  The frontier is
        factual (a strategy leaves it only on a real failure/inapplicability
        fact or a proven postcondition), so the same safe strategy may be
        requested again while the objective remains unfinished.
        """

        remaining_work = list(self._state.get("remaining_work") or [])
        if not remaining_work:
            return "", ""
        frontier = list(self._state.get("strategy_frontier") or [])
        for postcondition in remaining_work:
            for strategy_id in frontier:
                return str(strategy_id), str(postcondition)
        return "", ""

    def _next_reconciliation(self) -> tuple[str, str]:
        """Offer a read-only reconciliation segment for the first unknown effect.

        WOR-303: there is no reconciliation quota.  The offer repeats while a
        possible effect remains unreconciled; factual resolution (fresh
        evidence, verified effect, not-applied proof, ActionTruth exhaustion)
        is what ends the offer, never a counter.
        """

        unknown = self._unknown_effects_requiring_reconciliation()
        for unknown_key in sorted(unknown):
            return "reconcile_unknown_read_only", "reconcile:" + unknown_key
        return "", ""

    def _all_unknown_reconciliation_exhausted(self) -> bool:
        unknown = list((self._state.get("unknown_effects") or {}).values())
        return bool(unknown) and all(
            isinstance(item, dict)
            and str(item.get("reason_code") or "") in {
                "reconciliation_exhausted",
                "semantic_settle_exhausted",
            }
            for item in unknown
        )

    def mark_reconciliation_exhausted(
        self, attempt_id: str, *, evidence: object = "",
    ) -> bool:
        """Close only a read-only reconciliation frontier, never the effect."""

        key = _bounded(attempt_id, 192)
        with self._lock:
            self._refresh()
            unknown = self._state.setdefault("unknown_effects", {})
            item = unknown.get(key)
            if not key or not isinstance(item, dict):
                return False
            item["reason_code"] = "reconciliation_exhausted"
            item["reconciliation_evidence_token"] = (
                _fingerprint(evidence) if evidence not in (None, "") else ""
            )
            item["reconciliation_exhausted_at"] = _now()
            self._persist()
        reconciled = self.reconcile_tool_attempt(key)
        if reconciled:
            with self._lock:
                self._refresh()
                exhausted = (self._state.get("unknown_effects") or {}).get(key)
                if isinstance(exhausted, dict):
                    exhausted["reason_code"] = "reconciliation_exhausted"
                    exhausted["reconciliation_evidence_token"] = (
                        _fingerprint(evidence)
                        if evidence not in (None, "") else ""
                    )
                    exhausted["reconciliation_exhausted_at"] = _now()
                    self._persist()
        return True

    def _proven_blocker_reason(self) -> str:
        """Return a demonstrated factual terminal blocker, or an empty string.

        WOR-303: only causal, independently observed facts may terminate an
        unfinished objective.  The only provider-derived blocker is an
        explicitly non-retryable provider fact (credentials, configuration,
        model access, hard billing/quota, proven contract incompatibility)
        recorded from the escaped provider error itself.  Retryable transport
        errors and repeated provider prose never terminalize.  Legacy OSIA
        permission state carries no authority (WOR-301 removed the system).
        """

        provider_fact = str(self._state.get("provider_fact_blocker") or "")
        if provider_fact in _PROVEN_PROVIDER_FACT_BLOCKERS:
            return provider_fact
        return ""

    def _first_remaining_postcondition(self) -> str:
        remaining = list(self._state.get("remaining_work") or [])
        return str(remaining[0]) if remaining else ""

    def _next_replan(self, provider_route: str) -> str:
        """Offer one more factual continuation while safe work still remains.

        WOR-303: an unfinished objective always remains eligible for another
        concrete attempt.  There is no replan quota and no count-based
        terminal: the returned strategy id is a model-facing instruction, not
        a completion budget.
        """

        if not self._state.get("remaining_work"):
            return ""
        return _REPLAN_STRATEGY_ID

    def _reserve_blocker_terminal(self, reason: str) -> TerminalityDecision:
        """Retire the safe frontier for a proven blocker and report it."""

        if self._state.get("strategy_frontier"):
            self._state["strategy_frontier"] = []
        self._state["structural_wait_reason"] = ""
        self._persist()
        return TerminalityDecision(
            "terminal", TerminalState.BLOCKED_VERIFIED.value, reason,
        )

    def evaluate(self, *, informational_answer_satisfied: bool = False, provider_route: str = "default") -> TerminalityDecision:
        with self._lock:
            self._refresh()
            if self.terminal_state:
                return TerminalityDecision("terminal", self.terminal_state, self._state.get("terminal_reason", ""))
            self._reconcile_active_tool_attempts()
            cancellation = self._state.get("cancellation") or {}
            if cancellation.get("requested"):
                if (
                    not cancellation.get("effect_possible")
                    and not self._state.get("unknown_effects")
                    and not self._state.get("active_attempts")
                    and not any(
                        item.get("status") == "active"
                        for item in self._state.get("invocations") or []
                    )
                ):
                    return TerminalityDecision(
                        "terminal", TerminalState.USER_CANCELLED.value,
                        "authoritative_user_cancellation",
                    )
            if (
                not cancellation.get("requested")
                and self._state.get("hard_safety_reason")
            ):
                return TerminalityDecision(
                    "terminal", TerminalState.HARD_SAFETY_BLOCK.value,
                    str(self._state["hard_safety_reason"]),
                )
            if any(
                item.get("status") == "active"
                for item in self._state.get("invocations") or []
            ):
                return TerminalityDecision(
                    "wait",
                    reason_code="provider_continuation_active",
                    wait_type=WaitType.WAIT_PROVIDER_CONTINUATION.value,
                )
            if self._state.get("active_attempts"):
                return TerminalityDecision(
                    "wait",
                    reason_code="correlated_tool_attempt_active",
                    wait_type=WaitType.WAIT_PENDING_TOOL.value,
                )
            if cancellation.get("requested"):
                return TerminalityDecision(
                    "wait",
                    reason_code="cancellation_effect_requires_reconciliation",
                    wait_type=WaitType.WAIT_RECONCILIATION.value,
                )
            if self._state.get("outcome_contract"):
                blocker = self._proven_blocker_reason()
                if blocker:
                    return TerminalityDecision(
                        "terminal", TerminalState.BLOCKED_VERIFIED.value, blocker,
                    )
                if self._state.get("completion_text"):
                    return TerminalityDecision(
                        "terminal",
                        TerminalState.SUCCESS_VERIFIED.value,
                        "selected_model_completed",
                    )
                return TerminalityDecision("continue", reason_code="awaiting_selected_model")
            strategy_snapshot = self._state.get("strategy_snapshot") or {}
            desktop_observation = (
                strategy_snapshot.get("latest_desktop_observation") or {}
            )
            visual_failure_reason = str(
                desktop_observation.get("visual_failure_reason") or ""
            ) if isinstance(desktop_observation, dict) else ""
            if (
                isinstance(desktop_observation, dict)
                and visual_failure_reason in {
                    "CAPTURE_PROTECTED", "capture_protected_surface",
                }
            ):
                # The OS/application has authoritatively denied pixel access.
                # No accessibility, focus, or alternate Vision layer can make
                # that exact visual-read postcondition observable, so retire
                # the continuation frontier before reserving the durable final.
                self._state["strategy_frontier"] = []
                if not self._state.get("first_causal_error"):
                    self._state["first_causal_error"] = {
                        "reason_code": "CAPTURE_PROTECTED",
                        "evidence": {
                            "visual_failure_reason": visual_failure_reason,
                        },
                        "recorded_at": _now(),
                    }
                self._persist()
                return TerminalityDecision(
                    "terminal",
                    TerminalState.BLOCKED_VERIFIED.value,
                    "CAPTURE_PROTECTED",
                )
            if informational_answer_satisfied:
                # Answer text can satisfy only the built-in answer deliverable.
                # A legacy execution_required=False classification must never
                # turn an arbitrary external postcondition into a prose proof.
                postcondition = "a coherent answer directly satisfies the informational request"
                if self._state.get("required_postconditions") == [postcondition]:
                    self.prove_postcondition(postcondition, "coherent_informational_answer")
            if not self._state.get("remaining_work"):
                return TerminalityDecision(
                    "terminal", TerminalState.SUCCESS_VERIFIED.value,
                    "all_required_postconditions_proven",
                )
            # WOR-303: the objective is NOT complete.  Only a demonstrated,
            # causal blocker may reserve a blocked final.  Temporary frontier
            # emptiness, a prose-only provider segment, repeated provider
            # non-action and any counter/threshold continue the same canonical
            # turn with one more factual continuation instruction.
            blocker = self._proven_blocker_reason()
            if blocker:
                return self._reserve_blocker_terminal(blocker)
            strategy, postcondition = self._next_strategy(
                _bounded(provider_route, 160) or "default",
            )
            if strategy:
                return TerminalityDecision(
                    "continue", reason_code="safe_semantic_strategy_remains",
                    next_strategy=strategy, postcondition=postcondition,
                )
            replan = self._next_replan(
                _bounded(provider_route, 160) or "default",
            )
            if replan:
                return TerminalityDecision(
                    "continue",
                    reason_code=_REPLAN_REASON_CODE,
                    next_strategy=replan,
                    postcondition=self._first_remaining_postcondition(),
                )
            # No remaining work is reachable only when the objective is proven
            # complete; an unfinished objective therefore always continues.
            return TerminalityDecision(
                "continue",
                reason_code=_REPLAN_REASON_CODE,
                next_strategy=_REPLAN_STRATEGY_ID,
                postcondition=self._first_remaining_postcondition(),
            )

    @staticmethod
    def _bounded_goal_contract(state: dict) -> dict:
        """Project only the semantic essentials of the immutable contract.

        The durable contract may be large; continuation instructions must stay
        bounded so a long multi-round turn never overflows the provider context.
        """

        contract = state.get("goal_contract") or {}
        if not isinstance(contract, dict) or not contract:
            return {}
        nodes = []
        for raw in (contract.get("nodes") or [])[:12]:
            if not isinstance(raw, dict):
                continue
            nodes.append({
                "node_id": _bounded(raw.get("node_id"), 80),
                "description": _bounded(raw.get("description"), 400),
                "observable_postconditions": [
                    {"description": _bounded(item.get("description"), 300)}
                    for item in (raw.get("observable_postconditions") or [])[:6]
                    if isinstance(item, dict)
                ],
            })
        return {
            "root_objective": _bounded(contract.get("root_objective"), 2_000),
            "fingerprint": _bounded(state.get("goal_contract_fingerprint"), 128),
            "nodes": nodes,
        }

    def continuation_context(self, decision: TerminalityDecision) -> str:
        state = self.snapshot()
        if state.get("outcome_contract"):
            return "[Internal continuation of the same original objective. " + json.dumps({
                "objective": state["objective"],
                "outcomes": state["outcome_contract"],
                "completion_errors": state.get("completion_errors") or [],
                "factual_feedback": state.get("completion_feedback") or "",
                "receipts": [
                    {**item, "receipt_id": self._receipt_id(item)}
                    for item in state.get("typed_evidence") or []
                ],
                "unknown_effects": state.get("unknown_effects") or {},
                "active_attempts": state.get("active_attempts") or {},
                "instruction": "Choose the next action from these facts; repeating an action is allowed. "
                    "Compiler errors are feedback for repair. Complete all user-required results, "
                    "then answer normally, distinguishing observed results from uncertainty.",
            }, ensure_ascii=False) + "]"
        payload = {
            "event": "continue_same_user_turn",
            "turn_id": self.turn_id,
            "objective": _bounded(state["objective"], 4_000),
            "goal_contract": self._bounded_goal_contract(state),
            "goal_contract_fingerprint": str(
                state.get("goal_contract_fingerprint") or ""
            ),
            "proven_postconditions": state.get("proven_postconditions") or {},
            "remaining_work": state.get("remaining_work") or [],
            "recent_typed_evidence": (state.get("typed_evidence") or [])[-8:],
            "active_attempts": state.get("active_attempts") or {},
            "permission_state": state.get("permission_state") or "none",
            "provider_fact_blocker": state.get("provider_fact_blocker") or "",
            "unknown_effects": state.get("unknown_effects") or {},
            "attempt_history": (
                state.get("strategy_attempt_evidence") or []
            )[-32:],
            "next_strategy": decision.next_strategy,
            "target_postcondition": decision.postcondition,
        }
        if decision.next_strategy == "reconcile_unknown_read_only":
            strategy_snapshot = state.get("strategy_snapshot") or {}
            frozen_node_id = str(
                strategy_snapshot.get("frozen_node_id") or ""
            ) if isinstance(strategy_snapshot, dict) else ""
            contract = state.get("goal_contract") or {}
            contract_nodes = []
            if isinstance(contract, dict):
                contract_nodes = contract.get("nodes") or []
            frozen_contract_node = next((
                item for item in contract_nodes
                if isinstance(item, dict)
                and str(item.get("node_id") or "") == frozen_node_id
            ), {})
            postconditions = (
                frozen_contract_node.get("observable_postconditions") or []
                if isinstance(frozen_contract_node, dict) else []
            )
            checkpoint_postcondition = next((
                str(item.get("description") or "")
                for item in postconditions
                if isinstance(item, dict) and str(item.get("description") or "")
            ), "")
            payload["computer_use_v4_visual_evidence"] = {
                "instruction": (
                    "These facts describe the unknown outcome "
                    "of the intermediate checkpoint. If you choose a fresh "
                    "computer_screenshot, inspect it and call "
                    "computer_observation with that exact frame_id and status "
                    "satisfied, not_satisfied, or inconclusive. You may proceed "
                    "with another action or repeat an action. Provider prose alone is not evidence."
                ),
                "semantic_node_id": frozen_node_id,
                "intermediate_postcondition": checkpoint_postcondition,
            }
        if decision.next_strategy == _VERIFICATION_STRATEGY_ID:
            payload["read_only_verification"] = {
                "instruction": (
                    "Every productive step of the objective already ran and "
                    "only the whole-objective proof is missing. Use one "
                    "available read-only inspection capability on the exact "
                    "produced artifact or named target, then answer from what "
                    "it establishes. Repeating mutations is allowed; provider prose "
                    "alone is not verification evidence."
                ),
                "target_postcondition": decision.postcondition,
            }
        if decision.next_strategy == _REPLAN_STRATEGY_ID:
            strategy_snapshot = state.get("strategy_snapshot") or {}
            available_tools = sorted({
                _bounded(item, 128)
                for item in (
                    strategy_snapshot.get("available_tools")
                    if isinstance(strategy_snapshot, dict) else ()
                ) or ()
                if _bounded(item, 128)
            })
            first_causal = state.get("first_causal_error") or {}
            payload["replan"] = {
                "instruction": (
                    "The previous provider turn produced no material progress "
                    "on the remaining postcondition. Perform the next concrete "
                    "action now with one of the available capabilities. Do not "
                    "repeat an attempted call tuple without new evidence, do "
                    "not describe future work, and do not claim completion. If "
                    "a required capability or resource is genuinely missing, "
                    "say exactly what is missing instead of promising."
                ),
                "remaining_postcondition": (
                    decision.postcondition
                    or self._first_remaining_postcondition()
                ),
                "available_tools": available_tools,
                "first_causal_error": (
                    first_causal if isinstance(first_causal, dict) else {}
                ),
                "forensics_required": bool(
                    isinstance(first_causal, dict) and first_causal
                ),
            }
            if isinstance(first_causal, dict) and first_causal:
                payload["replan"]["forensics_instruction"] = (
                    "A concrete execution error is already recorded. "
                    "Investigate the causal blocker explicitly: state which "
                    "exact step is blocked, which capability was already "
                    "attempted and why it did not establish the postcondition, "
                    "then either perform a different safe action or report the "
                    "precise missing resource or user input."
                )
        return (
            "[Internal terminality continuation. This is the same user turn; "
            "the original objective remains authoritative. You may repeat previous "
            "steps, including after unknown outcomes. Provider prose is "
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
        bounded_reason = _bounded(reason_code, 160)
        with self._lock, self._process_lock():
            self._refresh()
            if self._state.get("final_reservation"):
                return False
            if self._state.get("outcome_contract"):
                decision = self.evaluate(provider_route=provider_route)
                if not decision.terminal or decision.terminal_state != state or decision.reason_code != bounded_reason:
                    return False
            else:  # Persisted legacy turns retain their historical reader.
                cancellation = self._state.get("cancellation") or {}
                if cancellation.get("requested"):
                    cancellation_terminal = bool(
                        state == TerminalState.USER_CANCELLED.value
                        and not cancellation.get("effect_possible")
                        and not self._state.get("unknown_effects")
                    )
                    unknown_terminal = bool(
                        state == TerminalState.BLOCKED_VERIFIED.value
                        and self._state.get("unknown_effects")
                        and bounded_reason in {
                            "durable_outcome_unknown",
                            "safe_reconciliation_frontier_exhausted",
                        }
                        and self._all_unknown_reconciliation_exhausted()
                    )
                    if not cancellation_terminal and not unknown_terminal:
                        return False
                if state == TerminalState.SUCCESS_VERIFIED.value and self._state.get("remaining_work"):
                    return False
                if state == TerminalState.USER_CANCELLED.value and not (
                    self._state.get("cancellation") or {}
                ).get("requested"):
                    return False
                if state == TerminalState.HARD_SAFETY_BLOCK.value and not self._state.get("hard_safety_reason"):
                    return False
                if state == TerminalState.BLOCKED_VERIFIED.value:
                    unknown_terminal = bool(
                        self._state.get("unknown_effects")
                        and bounded_reason in {
                            "durable_outcome_unknown",
                            "safe_reconciliation_frontier_exhausted",
                        }
                        and self._all_unknown_reconciliation_exhausted()
                    )
                    if (
                        self._state.get("active_attempts")
                        or self._state.get("strategy_frontier")
                        or (
                            self._state.get("unknown_effects")
                            and not unknown_terminal
                        )
                        or (
                            not unknown_terminal
                            and self._next_strategy(
                                _bounded(provider_route, 160) or "default"
                            ) != ("", "")
                        )
                    ):
                        return False
            self._state["terminal_state"] = state
            self._state["terminal_reason"] = bounded_reason
            self._state["final_reservation"] = {
                "reservation_id": "final_" + secrets.token_urlsafe(18),
                "reserved_at": _now(),
                "final_text": str(final_text or "")[:32_000],
            }
            self._persist()
            return True

    def attach_final_projection(
        self,
        *,
        execution_trace: dict | None = None,
        artifact_refs=None,
    ) -> None:
        """Durably bind sanitized UI/evidence metadata to the reserved final."""

        from osia.document_artifacts import normalize_references
        from osia.execution_trace import validate_persisted_trace

        safe_trace = validate_persisted_trace(execution_trace)
        safe_artifacts = normalize_references(artifact_refs)
        with self._lock, self._process_lock():
            self._refresh()
            reservation = self._state.get("final_reservation")
            binding = self._state.get("execution_binding") or {}
            if not isinstance(reservation, dict) or not reservation:
                raise ValueError("final reservation is required")
            if safe_trace is not None and any(
                str(safe_trace.get(key) or "") != str(binding.get(key) or "")
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            ):
                raise ValueError("final execution trace binding mismatch")
            existing_trace = reservation.get("execution_trace")
            if (
                existing_trace is not None
                and safe_trace is not None
                and existing_trace != safe_trace
            ):
                raise ValueError("final execution trace is immutable")
            existing_artifacts = normalize_references(
                reservation.get("artifact_refs")
            )
            if (
                existing_artifacts
                and safe_artifacts
                and existing_artifacts != safe_artifacts
            ):
                raise ValueError("final artifact references are immutable")
            if safe_trace is not None:
                reservation["execution_trace"] = safe_trace
            if safe_artifacts:
                reservation["artifact_refs"] = safe_artifacts
            self._persist()


__all__ = [
    "TERMINAL_STATES",
    "TerminalState",
    "TerminalityDecision",
    "TurnTerminalityController",
    "WaitType",
    "live_process_identity",
    "persisted_turn_state_exists",
]
