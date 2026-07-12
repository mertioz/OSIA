"""Sanitized Codex rate-limit snapshots and compact quota summaries."""

from __future__ import annotations

from copy import deepcopy
import re


_WINDOW_NAMES = ("primary", "secondary")
_WINDOW_FIELDS = ("usedPercent", "windowDurationMins", "resetsAt")
_SAFE_LIMIT_ID = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9._-]{1,64}$")


def _valid_int(value, *, minimum: int, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _sanitize_window(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    safe = {}
    used = raw.get("usedPercent")
    if _valid_int(used, minimum=0, maximum=100):
        safe["usedPercent"] = used
    duration = raw.get("windowDurationMins")
    if _valid_int(duration, minimum=1):
        safe["windowDurationMins"] = duration
    resets_at = raw.get("resetsAt")
    if _valid_int(resets_at, minimum=0):
        safe["resetsAt"] = resets_at
    return safe


def _sanitize_snapshot(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    safe = {}
    for name in _WINDOW_NAMES:
        window = _sanitize_window(raw.get(name))
        if window:
            safe[name] = window
    return safe


def sanitize_rate_limit_payload(payload) -> dict:
    """Keep only quota-window fields from a read response or update event."""
    if not isinstance(payload, dict):
        return {}
    source = payload
    data = payload.get("data")
    if (
        isinstance(data, dict)
        and not any(key in payload for key in ("rateLimits", "rateLimitsByLimitId"))
    ):
        source = data

    raw_legacy = source.get("rateLimits")
    if not isinstance(raw_legacy, dict) and any(name in source for name in _WINDOW_NAMES):
        raw_legacy = source
    safe = {}
    legacy = _sanitize_snapshot(raw_legacy)
    if legacy:
        safe["rateLimits"] = legacy

    raw_by_id = source.get("rateLimitsByLimitId")
    if isinstance(raw_by_id, dict):
        by_id = {}
        for raw_limit_id, raw_snapshot in raw_by_id.items():
            limit_id = str(raw_limit_id or "").strip()
            snapshot = _sanitize_snapshot(raw_snapshot)
            if _SAFE_LIMIT_ID.fullmatch(limit_id) and snapshot:
                by_id[limit_id] = snapshot
        if by_id:
            safe["rateLimitsByLimitId"] = by_id
    return safe


def _merge_window(base, update) -> dict:
    merged = dict(base) if isinstance(base, dict) else {}
    if isinstance(update, dict):
        for field in _WINDOW_FIELDS:
            if field in update:
                merged[field] = update[field]
    return merged


def _merge_snapshot(base, update) -> dict:
    merged = deepcopy(base) if isinstance(base, dict) else {}
    if isinstance(update, dict):
        for name in _WINDOW_NAMES:
            if name in update:
                merged[name] = _merge_window(merged.get(name), update[name])
    return merged


def merge_rate_limit_payload(base, update) -> dict:
    """Merge a sparse ``account/rateLimits/updated`` payload into a snapshot."""
    merged = sanitize_rate_limit_payload(base)
    incoming = sanitize_rate_limit_payload(update)
    old_legacy = deepcopy(merged.get("rateLimits"))

    legacy_update = incoming.get("rateLimits")
    if isinstance(legacy_update, dict):
        merged["rateLimits"] = _merge_snapshot(old_legacy, legacy_update)

        # The backward-compatible view mirrors the normal ``codex`` bucket.
        # Fall back to an unambiguous match for older or renamed single buckets.
        by_id = merged.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and by_id:
            target = "codex" if "codex" in by_id else None
            if target is None:
                matches = [key for key, value in by_id.items() if value == old_legacy]
                if len(matches) == 1:
                    target = matches[0]
                elif len(by_id) == 1:
                    target = next(iter(by_id))
            if target is not None:
                by_id[target] = _merge_snapshot(by_id.get(target), legacy_update)

    incoming_by_id = incoming.get("rateLimitsByLimitId")
    if isinstance(incoming_by_id, dict):
        by_id = merged.setdefault("rateLimitsByLimitId", {})
        for limit_id, snapshot in incoming_by_id.items():
            by_id[limit_id] = _merge_snapshot(by_id.get(limit_id), snapshot)
    return merged


def _duration_label(minutes: int | None, fallback: str) -> str:
    if minutes == 10080:
        return "Weekly"
    if _valid_int(minutes, minimum=1):
        if minutes % 1440 == 0:
            return f"{minutes // 1440} d"
        if minutes % 60 == 0:
            return f"{minutes // 60} h"
        return f"{minutes} min"
    return fallback


def quota_windows(payload) -> list[dict]:
    """Return canonical UI windows with explicit remaining percentages."""
    safe = sanitize_rate_limit_payload(payload)
    snapshot = safe.get("rateLimits")
    if not isinstance(snapshot, dict):
        return []

    windows = []
    for name, fallback in (("primary", "Primary"), ("secondary", "Secondary")):
        window = snapshot.get(name)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if not _valid_int(used, minimum=0, maximum=100):
            continue
        label = _duration_label(window.get("windowDurationMins"), fallback)
        windows.append({"label": label, "remainingPercent": 100 - used})
    return windows
