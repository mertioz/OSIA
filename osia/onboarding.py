"""Versioned first-install onboarding and semantic Guide progress state.

Only an installer with deterministic evidence of a clean profile creates the
pending marker.  Missing or corrupt state therefore never opts an established
user back into automatic onboarding.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from osia.config import CONFIG_DIR
from osia.guide_catalog import (
    CATALOG_SCHEMA_VERSION,
    TRACK_ADVANCED,
    TRACK_ESSENTIAL,
    TRACK_MASTERY,
    legacy_children,
    load_catalog,
    resolve_slide_id,
    slide_by_id,
    slides_for_track,
)


ONBOARDING_STATE_VERSION = 2
ONBOARDING_STATE_PATH = Path(CONFIG_DIR) / "onboarding.json"
_SUPPORTED_STATE_VERSIONS = frozenset({1, ONBOARDING_STATE_VERSION})
_HANDLED_OUTCOMES = frozenset({"completed", "skipped", "closed", "migrated"})
_UNSET = object()
_TRACK_IDS = (TRACK_ESSENTIAL, TRACK_MASTERY, TRACK_ADVANCED)


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
        state.get("schemaVersion") in _SUPPORTED_STATE_VERSIONS
        and state.get("status") in {"pending", "presenting", "handled"}
    )


def _track_for_slide(slide_id: str | None) -> str | None:
    slide = slide_by_id(slide_id)
    return slide.track_id if slide is not None else None


def _migrate_ids(raw: object, track_id: str) -> list[str]:
    result: list[str] = []
    if not isinstance(raw, list):
        return result
    allowed = {slide.id for slide in slides_for_track(track_id)}
    for value in raw:
        if not isinstance(value, str):
            continue
        for candidate in legacy_children(value):
            if candidate in allowed and candidate not in result:
                result.append(candidate)
    return result


def _progress_from_state(state: dict) -> dict:
    """Return bounded v4 progress without opting anyone into first-run UI."""

    visited_raw = state.get("visitedByTrack")
    if isinstance(visited_raw, dict):
        visited = {
            track_id: _migrate_ids(visited_raw.get(track_id), track_id)
            for track_id in _TRACK_IDS
        }
    else:
        essential = []
        if state.get("essentialCompleted") is True:
            essential = [slide.id for slide in slides_for_track(TRACK_ESSENTIAL)]
        content_version = state.get("contentSchemaVersion")
        if not isinstance(content_version, int) or content_version < 3:
            mastery = _migrate_ids(state.get("advancedCompleted"), TRACK_MASTERY)
            advanced: list[str] = []
        else:
            mastery = _migrate_ids(state.get("masteryCompleted"), TRACK_MASTERY)
            advanced = _migrate_ids(state.get("advancedCompleted"), TRACK_ADVANCED)
        visited = {
            TRACK_ESSENTIAL: essential,
            TRACK_MASTERY: mastery,
            TRACK_ADVANCED: advanced,
        }

    last_raw = state.get("lastSlideByTrack")
    last_by_track: dict[str, str] = {}
    if isinstance(last_raw, dict):
        for track_id in _TRACK_IDS:
            value = last_raw.get(track_id)
            resolved = resolve_slide_id(value, track_id=track_id) if isinstance(value, str) else None
            if resolved and _track_for_slide(resolved) == track_id:
                last_by_track[track_id] = resolved

    legacy_current = state.get("currentLesson")
    current = resolve_slide_id(legacy_current) if isinstance(legacy_current, str) else None
    current_track = _track_for_slide(current)
    if current and current_track:
        last_by_track.setdefault(current_track, current)

    last_track = state.get("lastTrackId")
    if last_track not in _TRACK_IDS:
        last_track = current_track or TRACK_ESSENTIAL
    if last_track not in last_by_track:
        first = slides_for_track(last_track)[0]
        last_by_track[last_track] = first.id
    current = last_by_track[last_track]

    return {
        "contentVersion": load_catalog().content_version,
        "contentSchemaVersion": 3,
        "lastTrackId": last_track,
        "lastSlideByTrack": last_by_track,
        "visitedByTrack": visited,
        "currentLesson": current,
        "essentialCompleted": len(visited[TRACK_ESSENTIAL]) == len(slides_for_track(TRACK_ESSENTIAL)),
        "masteryCompleted": visited[TRACK_MASTERY],
        "advancedCompleted": visited[TRACK_ADVANCED],
        "skipped": state.get("skipped") is True,
        "finished": state.get("finished") is True,
    }


def onboarding_state(path: Path | None = None) -> dict:
    """Return validated onboarding state, or empty data for absent/corrupt data."""

    state = _load(_path(path))
    return dict(state) if _valid_base_state(state) else {}


def guide_progress(path: Path | None = None) -> dict:
    """Return resumable Guide progress with safe existing-user defaults."""

    return _progress_from_state(onboarding_state(path))


def update_guide_progress(
    path: Path | None = None,
    *,
    current_lesson=_UNSET,
    last_track_id=_UNSET,
    last_slide_by_track=_UNSET,
    visited_by_track=_UNSET,
    essential_completed=_UNSET,
    mastery_completed=_UNSET,
    advanced_completed=_UNSET,
    skipped=_UNSET,
    finished=_UNSET,
) -> dict:
    """Persist Guide-only state while preserving onboarding eligibility."""

    target = _path(path)
    state = onboarding_state(target)
    if not state:
        state = {
            "schemaVersion": ONBOARDING_STATE_VERSION,
            "status": "handled",
            "outcome": "migrated",
        }
    else:
        state["schemaVersion"] = ONBOARDING_STATE_VERSION
    progress = _progress_from_state(state)

    visited = {key: list(values) for key, values in progress["visitedByTrack"].items()}
    if visited_by_track is not _UNSET and isinstance(visited_by_track, dict):
        visited = {
            track_id: _migrate_ids(visited_by_track.get(track_id), track_id)
            for track_id in _TRACK_IDS
        }
    if essential_completed is not _UNSET:
        visited[TRACK_ESSENTIAL] = (
            [slide.id for slide in slides_for_track(TRACK_ESSENTIAL)]
            if bool(essential_completed)
            else []
        )
    if mastery_completed is not _UNSET:
        visited[TRACK_MASTERY] = _migrate_ids(list(mastery_completed) if isinstance(mastery_completed, (tuple, set)) else mastery_completed, TRACK_MASTERY)
    if advanced_completed is not _UNSET:
        visited[TRACK_ADVANCED] = _migrate_ids(list(advanced_completed) if isinstance(advanced_completed, (tuple, set)) else advanced_completed, TRACK_ADVANCED)

    last_by_track = dict(progress["lastSlideByTrack"])
    if last_slide_by_track is not _UNSET and isinstance(last_slide_by_track, dict):
        last_by_track = {}
        for track_id in _TRACK_IDS:
            value = last_slide_by_track.get(track_id)
            resolved = resolve_slide_id(value, track_id=track_id) if isinstance(value, str) else None
            if resolved and _track_for_slide(resolved) == track_id:
                last_by_track[track_id] = resolved
    if current_lesson is not _UNSET and isinstance(current_lesson, str):
        resolved = resolve_slide_id(current_lesson)
        track_id = _track_for_slide(resolved)
        if resolved and track_id:
            last_by_track[track_id] = resolved
            progress["lastTrackId"] = track_id
    if last_track_id is not _UNSET and last_track_id in _TRACK_IDS:
        progress["lastTrackId"] = last_track_id
    selected_track = progress["lastTrackId"]
    if selected_track not in last_by_track:
        last_by_track[selected_track] = slides_for_track(selected_track)[0].id

    progress.update({
        "contentVersion": load_catalog().content_version,
        "contentSchemaVersion": 3,
        "lastSlideByTrack": last_by_track,
        "visitedByTrack": visited,
        "currentLesson": last_by_track[selected_track],
        "essentialCompleted": len(visited[TRACK_ESSENTIAL]) == len(slides_for_track(TRACK_ESSENTIAL)),
        "masteryCompleted": visited[TRACK_MASTERY],
        "advancedCompleted": visited[TRACK_ADVANCED],
    })
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
        claimed["schemaVersion"] = ONBOARDING_STATE_VERSION
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


def mark_onboarding_handled(outcome: str, path: Path | None = None) -> None:
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
