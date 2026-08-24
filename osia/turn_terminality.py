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
        semantic_objective = _bounded(objective, 32_000)
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
                    "unknown_effects": {},
                    "unknown_effect_resolutions": [],
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
        the terminality state already owns an exact active ToolAttemptStore ID.
        Remove only that redundant provider-process barrier; uncertainty about
        the action itself remains owned by the durable attempt and its bounded
        read-only reconciliation path.
        """

        key = _bounded(attempt_id, 192)
        if not key:
            raise ValueError("tool attempt id is required")
        with self._lock, self._process_lock():
            self._refresh()
            if (
                self.terminal_state
                or self._state.get("final_reservation")
                or key not in (self._state.get("active_attempts") or {})
            ):
                raise ValueError("durable tool attempt is not resumable")
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
            self._bind_strategy_unknown_effects(safe)
            self._resolve_strategy_unknown_effects(safe)
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
            self._state["typed_evidence"] = typed[-128:]
            tokens = self._state.setdefault("evidence_tokens", [])
            if evidence_token not in tokens:
                tokens.append(evidence_token)
                self._state["evidence_tokens"] = tokens[-128:]
                self._state["evidence_revision"] = self.evidence_revision + 1
            self._state["progress_revision"] = self.progress_revision + 1
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
                if entry["reason_code"] in {
                    "reconciliation_exhausted",
                    "semantic_settle_exhausted",
                }:
                    self._state.setdefault("active_attempts", {}).pop(
                        key, None,
                    )
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
        contract = getattr(execution_record, "contract", None)
        side_effect = str(
            getattr(
                getattr(contract, "side_effect", None),
                "value",
                getattr(contract, "side_effect", ""),
            )
            or ""
        )
        if outcome == "success_unverified" and side_effect == "read_only":
            outcome = "success_verified"
            reason_code = reason_code or "read_only_result_returned"
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

    def try_request_cancellation(self, *, effect_possible: bool) -> bool:
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
            self._state.setdefault("active_attempts", {}).pop(key, None)
            if (
                self._state.get("permission_state") == "pending"
                and not self._state.get("active_attempts")
            ):
                self._state["permission_state"] = "resolved"
            self._persist()
            return True

    def evaluate(self, *, informational_answer_satisfied: bool = False, provider_route: str = "default") -> TerminalityDecision:
        with self._lock:
            self._refresh()
            if self.terminal_state:
                return TerminalityDecision("terminal", self.terminal_state, self._state.get("terminal_reason", ""))
            cancellation = self._state.get("cancellation") or {}
            if cancellation.get("requested"):
                if (
                    not cancellation.get("effect_possible")
                    and not self._state.get("unknown_effects")
                    and not self._state.get("active_attempts")
                    and self._state.get("permission_state") != "pending"
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
                if (
                    self._all_unknown_reconciliation_exhausted()
                    and not self._state.get("strategy_frontier")
                ):
                    return TerminalityDecision(
                        "terminal",
                        TerminalState.BLOCKED_VERIFIED.value,
                        "safe_reconciliation_frontier_exhausted",
                    )
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
            if cancellation.get("requested"):
                return TerminalityDecision(
                    "wait",
                    reason_code="cancellation_effect_requires_reconciliation",
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
            "goal_contract": state.get("goal_contract") or {},
            "goal_contract_fingerprint": str(
                state.get("goal_contract_fingerprint") or ""
            ),
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
        bounded_reason = _bounded(reason_code, 160)
        with self._lock, self._process_lock():
            self._refresh()
            if self._state.get("final_reservation"):
                return False
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
                    or self._state.get("permission_state") == "pending"
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
    "live_process_identity",
    "persisted_turn_state_exists",
]
