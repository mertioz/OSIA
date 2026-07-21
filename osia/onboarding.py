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
from osia.guide_content import (
    CATEGORY_ADVANCED,
    GUIDE_CONTENT_SCHEMA_VERSION,
    LESSONS_BY_ID,
)


ONBOARDING_STATE_VERSION = 1
ONBOARDING_STATE_PATH = Path(CONFIG_DIR) / "onboarding.json"
_HANDLED_OUTCOMES = frozenset({"completed", "skipped", "closed", "migrated"})
_UNSET = object()


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


def _valid_base_state(state: dict) -> bool:
    return (
        state.get("schemaVersion") == ONBOARDING_STATE_VERSION
        and state.get("status") in {"pending", "presenting", "handled"}
    )


def _progress_from_state(state: dict) -> dict:
    """Return bounded guide progress without opting anyone into first-run UI."""

    lesson = state.get("currentLesson")
    if not isinstance(lesson, str) or lesson not in LESSONS_BY_ID:
        lesson = ""
    raw_advanced = state.get("advancedCompleted", [])
    advanced: list[str] = []
    if isinstance(raw_advanced, list):
        for value in raw_advanced:
            registered = LESSONS_BY_ID.get(value) if isinstance(value, str) else None
            if (
                registered is not None
                and registered.category == CATEGORY_ADVANCED
                and value not in advanced
            ):
                advanced.append(value)
    return {
        "contentSchemaVersion": GUIDE_CONTENT_SCHEMA_VERSION,
        "essentialCompleted": state.get("essentialCompleted") is True,
        "currentLesson": lesson,
        "advancedCompleted": advanced,
        "skipped": state.get("skipped") is True,
        "finished": state.get("finished") is True,
    }


def onboarding_state(path: Path | None = None) -> dict:
    """Return the validated state, or an empty dict for absent/legacy data."""

    state = _load(_path(path))
    if not _valid_base_state(state):
        return {}
    return dict(state)


def guide_progress(path: Path | None = None) -> dict:
    """Return resumable guide progress with safe defaults for existing users."""

    return _progress_from_state(onboarding_state(path))


def update_guide_progress(
    path: Path | None = None,
    *,
    current_lesson=_UNSET,
    essential_completed=_UNSET,
    advanced_completed=_UNSET,
    skipped=_UNSET,
    finished=_UNSET,
) -> dict:
    """Persist guide-only state without changing application configuration.

    An existing user may open the guide without ever having received an
    installer marker.  Their first progress write creates a ``handled`` /
    ``migrated`` record, which can never trigger automatic onboarding.
    """

    target = _path(path)
    state = onboarding_state(target)
    if not state:
        state = {
            "schemaVersion": ONBOARDING_STATE_VERSION,
            "status": "handled",
            "outcome": "migrated",
        }
    progress = _progress_from_state(state)
    if current_lesson is not _UNSET:
        progress["currentLesson"] = (
            current_lesson
            if isinstance(current_lesson, str) and current_lesson in LESSONS_BY_ID
            else ""
        )
    if essential_completed is not _UNSET:
        progress["essentialCompleted"] = bool(essential_completed)
    if advanced_completed is not _UNSET:
        values: list[str] = []
        if isinstance(advanced_completed, (list, tuple, set)):
            for value in advanced_completed:
                registered = (
                    LESSONS_BY_ID.get(value) if isinstance(value, str) else None
                )
                if (
                    registered is not None
                    and registered.category == CATEGORY_ADVANCED
                    and value not in values
                ):
                    values.append(value)
        progress["advancedCompleted"] = values
    if skipped is not _UNSET:
        progress["skipped"] = bool(skipped)
    if finished is not _UNSET:
        progress["finished"] = bool(finished)
    state.update(progress)
    _save(target, state)
    return dict(progress)


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
                migrated = {
                    "schemaVersion": ONBOARDING_STATE_VERSION,
                    "status": "handled",
                    "outcome": "migrated",
                }
                migrated.update(_progress_from_state({}))
                _save(target, migrated)
            return False
        claimed = dict(state)
        claimed["status"] = "presenting"
        claimed.pop("outcome", None)
        claimed.update(_progress_from_state(claimed))
        _save(target, claimed)
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
    target = _path(path)
    state = onboarding_state(target)
    progress = _progress_from_state(state)
    if outcome == "skipped":
        progress["skipped"] = True
    elif outcome == "completed":
        progress["finished"] = True
    handled = {
        "schemaVersion": ONBOARDING_STATE_VERSION,
        "status": "handled",
        "outcome": outcome,
    }
    handled.update(progress)
    _save(target, handled)


__all__ = [
    "ONBOARDING_STATE_PATH",
    "ONBOARDING_STATE_VERSION",
    "claim_automatic_onboarding",
    "create_fresh_install_marker",
    "guide_progress",
    "mark_onboarding_handled",
    "onboarding_state",
    "update_guide_progress",
]
