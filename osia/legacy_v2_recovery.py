"""Read-only compatibility for interrupted legacy V2 turn identities.

New Chat turns never enter a V2 runtime. This module only detects a persisted
legacy diagnostic for the exact old turn ID and projects an interruption
result so the canonical dispatcher cannot mistake it for a fresh turn.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path

from osia.config import CONFIG_DIR
from osia.turn_terminality import TurnTerminalityController, _now

RUNTIME = "semantic_execution_loop_v2"


def _diagnostic_path(turn_id: str, root=None) -> Path:
    directory = Path(root) if root is not None else Path(CONFIG_DIR) / "semantic_v2_diagnostics"
    return directory / (hashlib.sha256(str(turn_id).encode("utf-8")).hexdigest() + ".json")


def _read_diagnostic(turn_id: str) -> dict:
    try:
        value = json.loads(_diagnostic_path(turn_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def known_v2_turn(turn_id: str) -> bool:
    """Identify only the same persisted legacy turn; never route a new turn."""
    return _diagnostic_path(turn_id).is_file()


def recover_interrupted_v2_final(turn_id: str, objective: str, binding: dict):
    """Project the old turn's interruption without invoking an alternate loop."""
    if not known_v2_turn(turn_id):
        return None

    diagnostic = _read_diagnostic(turn_id)
    controller = TurnTerminalityController(
        turn_id=turn_id,
        objective=objective,
        required_postconditions=("original_objective_satisfied",),
        execution_required=True,
    )
    controller.set_execution_binding(binding)
    boundary = str(diagnostic.get("boundary") or "legacy_v2_interrupted")[:120]
    final_text = (
        "The legacy turn was interrupted; completion is unverified. "
        "Continue with a new Chat request if you want to proceed."
    )
    with controller._lock, controller._process_lock():
        controller._refresh()
        if not controller._state.get("final_reservation"):
            controller._state["execution_runtime"] = RUNTIME
            controller._state["v2_failure_boundary"] = boundary
            controller._state["terminal_state"] = "blocked_verified"
            controller._state["terminal_reason"] = "legacy_v2_interrupted"
            controller._state["final_reservation"] = {
                "reservation_id": "legacy_" + secrets.token_urlsafe(18),
                "reserved_at": _now(),
                "final_text": final_text,
            }
            controller._persist()
    return controller


def v2_failure_text(snapshot: dict, lang: str) -> str:
    french = str(lang).startswith("fr")
    if snapshot.get("unknown_effects"):
        return (
            "Une action précédente reste non vérifiée; aucune réussite n'est déclarée."
            if french else
            "A prior action remains unverified; no success is being claimed."
        )
    boundary = str(snapshot.get("v2_failure_boundary") or "")
    if boundary in {"application_selection", "application_binding", "initial_observation"}:
        return (
            "La fenêtre de l'application n'a pas pu être vérifiée."
            if french else
            "The requested application window could not be verified."
        )
    return (
        "Le tour hérité a été interrompu avant vérification de son résultat."
        if french else
        "The legacy turn was interrupted before its result was verified."
    )
