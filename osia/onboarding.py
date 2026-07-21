"""Versioned first-install onboarding state.

Only an installer with deterministic evidence of a clean user profile creates
the pending marker.  A missing marker therefore means an existing/migrated
user and never opts them into onboarding implicitly.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from osia.config import CONFIG_DIR


ONBOARDING_STATE_VERSION = 1
ONBOARDING_STATE_PATH = Path(CONFIG_DIR) / "onboarding.json"
_HANDLED_OUTCOMES = frozenset({"completed", "skipped", "closed", "migrated"})


def _path(path: Path | None) -> Path:
    return Path(path) if path is not None else ONBOARDING_STATE_PATH


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create_fresh_install_marker(path: Path | None = None) -> bool:
    """Create ``pending`` only when the target profile has no prior state."""

    target = _path(path)
    if target.exists():
        return False
    try:
        existing = [entry for entry in target.parent.iterdir() if entry != target]
    except FileNotFoundError:
        existing = []
    except OSError:
        return False
    if existing:
        return False
    _save(target, {"schemaVersion": ONBOARDING_STATE_VERSION, "status": "pending"})
    return True


def onboarding_state(path: Path | None = None) -> dict:
    """Return the validated state, or an empty dict for absent/legacy data."""

    state = _load(_path(path))
    if state.get("schemaVersion") != ONBOARDING_STATE_VERSION:
        return {}
    if state.get("status") not in {"pending", "presenting", "handled"}:
        return {}
    return dict(state)


def claim_automatic_onboarding(path: Path | None = None) -> bool:
    """Atomically consume one pending first launch before showing the UI."""

    target = _path(path)
    claim = target.with_name(target.name + ".claim")
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    try:
        os.close(descriptor)
        state = onboarding_state(target)
        if state.get("status") != "pending":
            if not target.exists():
                _save(
                    target,
                    {
                        "schemaVersion": ONBOARDING_STATE_VERSION,
                        "status": "handled",
                        "outcome": "migrated",
                    },
                )
            return False
        _save(
            target,
            {"schemaVersion": ONBOARDING_STATE_VERSION, "status": "presenting"},
        )
        return True
    finally:
        try:
            claim.unlink()
        except OSError:
            pass


def mark_onboarding_handled(
    outcome: str, path: Path | None = None,
) -> None:
    """Persist completion, skip, close, or existing-user migration."""

    if outcome not in _HANDLED_OUTCOMES:
        raise ValueError("invalid_onboarding_outcome")
    _save(
        _path(path),
        {
            "schemaVersion": ONBOARDING_STATE_VERSION,
            "status": "handled",
            "outcome": outcome,
        },
    )


__all__ = [
    "ONBOARDING_STATE_PATH",
    "ONBOARDING_STATE_VERSION",
    "claim_automatic_onboarding",
    "create_fresh_install_marker",
    "mark_onboarding_handled",
    "onboarding_state",
]
