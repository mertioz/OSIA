#!/usr/bin/env python3
"""Local persistent memory store for OSIA.

Phase A is intentionally storage-only: no agent, UI, server, or mobile wiring.
The default store lives at ``CONFIG_DIR / "memory.json"`` and can be overridden
in tests by passing an explicit path to :class:`MemoryStore`.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import sys
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from osia.config import CONFIG_DIR as _CONFIG_DIR


SCHEMA_VERSION = 1
MEMORY_FILE = Path(_CONFIG_DIR) / "memory.json"

ALLOWED_TYPES = frozenset({
    "route",
    "preference",
    "fact",
    "workflow",
    "contact",
    "shortcut",
})
ALLOWED_CATEGORIES = frozenset({
    "app",
    "site",
    "deep_link",
    "folder",
    "file",
    "game",
    "user",
    "workflow",
    "contact",
    "other",
})
ALLOWED_VALUE_KINDS = frozenset({
    "path",
    "url",
    "uri",
    "steam_appid",
    "text",
    "steps",
})

_ENTRY_ID_RE = re.compile(r"^mem_[A-Za-z0-9_-]{6,}$")
_OPENAI_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}")
_GITHUB_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"
)
_SLACK_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])xox[abprs]-[A-Za-z0-9-]{20,}")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_LABEL_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(?:access[_-]?token|refresh[_-]?token|oauth[_-]?token|"
    r"api[_-]?key|client[_-]?secret|password)\b\s*[:=]\s*"
    r"[\"']?[A-Za-z0-9._~+/=-]{12,}"
)
_SECRET_FIELD_RE = re.compile(
    r"(?i)^(?:access[_-]?token|accessToken|refresh[_-]?token|refreshToken|"
    r"oauth[_-]?token|oauthToken|api[_-]?key|apiKey|client[_-]?secret|"
    r"clientSecret|password|secret)$"
)
_ENTROPY_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]{48,}")
_SKIP_SECRET_KEYS = frozenset({
    "id",
    "created_at",
    "updated_at",
    "last_used_at",
    "expires_at",
    "schema_version",
})


class MemoryError(Exception):
    """Base class for memory-store errors."""


class MemoryValidationError(MemoryError, ValueError):
    """Raised when a memory entry does not match the Phase A schema."""


class MemorySecretError(MemoryValidationError):
    """Raised when an entry appears to contain a secret or token."""


@dataclass
class MemoryEntry:
    """Schema-version-1 memory entry."""

    id: str = ""
    type: str = "fact"
    category: str = "other"
    key: str = ""
    title: str = ""
    value: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    source: str = "explicit_user"
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str | None = None
    tags: list[str] = field(default_factory=list)
    device: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: copy.deepcopy(v) for k, v in data.items() if k in allowed})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _strip_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_tags = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_tags = list(value)
    else:
        raw_tags = []
    out: list[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        clean = _strip_text(tag)
        folded = clean.casefold()
        if clean and folded not in seen:
            out.append(clean)
            seen.add(folded)
    return out


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 1.0
    if math.isnan(confidence) or math.isinf(confidence):
        return 1.0
    return max(0.0, min(1.0, confidence))


def _jsonable_copy(value: Any, field_name: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(f"{field_name} must be JSON serializable") from exc
    return copy.deepcopy(value)


def _has_required_data(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _flatten_secret_text(value: Any, *, parent_key: str = "") -> list[str]:
    if isinstance(value, dict):
        texts: list[str] = []
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key in _SKIP_SECRET_KEYS:
                continue
            if _SECRET_FIELD_RE.search(key) and _has_required_data(raw_value):
                raise MemorySecretError(
                    f"memory entry contains secret-like field: {key}"
                )
            texts.extend(_flatten_secret_text(raw_value, parent_key=key))
        return texts
    if isinstance(value, (list, tuple, set)):
        texts = []
        for item in value:
            texts.extend(_flatten_secret_text(item, parent_key=parent_key))
        return texts
    if isinstance(value, str):
        return [value]
    if value is None:
        return []
    return [str(value)]


def _entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _looks_high_entropy(token: str) -> bool:
    classes = 0
    classes += any(c.islower() for c in token)
    classes += any(c.isupper() for c in token)
    classes += any(c.isdigit() for c in token)
    classes += any(c in "+/=_-" for c in token)
    return len(token) >= 48 and classes >= 3 and _entropy(token) >= 4.3


def reject_secret_like_entry(entry: MemoryEntry | dict[str, Any]) -> None:
    """Raise when an entry appears to contain an API key, token, or password."""

    data = entry.to_dict() if isinstance(entry, MemoryEntry) else copy.deepcopy(entry)
    if not isinstance(data, dict):
        return
    texts = _flatten_secret_text(data)
    for text in texts:
        if _PRIVATE_KEY_RE.search(text):
            raise MemorySecretError("memory entry contains a private key block")
        if _OPENAI_KEY_RE.search(text):
            raise MemorySecretError("memory entry contains an OpenAI-style API key")
        if _GITHUB_KEY_RE.search(text):
            raise MemorySecretError("memory entry contains a GitHub token")
        if _SLACK_KEY_RE.search(text):
            raise MemorySecretError("memory entry contains a Slack token")
        if _JWT_RE.search(text):
            raise MemorySecretError("memory entry contains a JWT-like token")
        if _LABEL_VALUE_SECRET_RE.search(text):
            raise MemorySecretError("memory entry contains a secret-like assignment")
        for candidate in _ENTROPY_TOKEN_RE.findall(text):
            if _looks_high_entropy(candidate):
                raise MemorySecretError("memory entry contains a high-entropy token")


def validate_entry(
    entry: MemoryEntry | dict[str, Any],
    *,
    existing: MemoryEntry | dict[str, Any] | None = None,
    touch_updated: bool = True,
) -> MemoryEntry:
    """Normalize and validate one schema-version-1 memory entry."""

    raw = entry.to_dict() if isinstance(entry, MemoryEntry) else copy.deepcopy(entry)
    if not isinstance(raw, dict):
        raise MemoryValidationError("memory entry must be an object")

    existing_data = (
        existing.to_dict() if isinstance(existing, MemoryEntry) else copy.deepcopy(existing)
    )
    if existing_data is not None and not isinstance(existing_data, dict):
        existing_data = None

    now = _now_utc()
    entry_id = _strip_text(raw.get("id"))
    if not entry_id:
        entry_id = f"mem_{uuid.uuid4().hex}"
    if not _ENTRY_ID_RE.match(entry_id):
        raise MemoryValidationError("id must be empty or look like mem_<id>")

    memory_type = _strip_text(raw.get("type") or "fact").lower()
    if memory_type not in ALLOWED_TYPES:
        raise MemoryValidationError(f"type must be one of {sorted(ALLOWED_TYPES)}")

    category = _strip_text(raw.get("category") or "other").lower()
    if category not in ALLOWED_CATEGORIES:
        raise MemoryValidationError(
            f"category must be one of {sorted(ALLOWED_CATEGORIES)}"
        )

    key = _strip_text(raw.get("key")).lower()
    title = _strip_text(raw.get("title"))
    if not key:
        raise MemoryValidationError("key is required")
    if not title:
        raise MemoryValidationError("title is required")

    value = raw.get("value")
    if not isinstance(value, dict):
        raise MemoryValidationError("value must be an object")
    kind = _strip_text(value.get("kind")).lower()
    if kind not in ALLOWED_VALUE_KINDS:
        raise MemoryValidationError(
            f"value.kind must be one of {sorted(ALLOWED_VALUE_KINDS)}"
        )
    if "data" not in value or not _has_required_data(value.get("data")):
        raise MemoryValidationError("value.data is required")
    clean_value = {
        "kind": kind,
        "data": _jsonable_copy(value.get("data"), "value.data"),
    }

    created_at = _strip_text(raw.get("created_at"))
    if existing_data:
        created_at = _strip_text(existing_data.get("created_at")) or created_at
    created_at = created_at or now
    if touch_updated:
        updated_at = now
    else:
        updated_at = _strip_text(raw.get("updated_at")) or now

    last_used_at = _strip_text(raw.get("last_used_at")) or None
    expires_at = _strip_text(raw.get("expires_at")) or None
    device = raw.get("device") if isinstance(raw.get("device"), dict) else {}

    normalized = MemoryEntry(
        id=entry_id,
        type=memory_type,
        category=category,
        key=key,
        title=title,
        value=clean_value,
        confidence=_clamp_confidence(raw.get("confidence", 1.0)),
        source=_strip_text(raw.get("source")) or "explicit_user",
        created_at=created_at,
        updated_at=updated_at,
        last_used_at=last_used_at,
        tags=_normalize_tags(raw.get("tags")),
        device=_jsonable_copy(device, "device"),
        expires_at=expires_at,
    )
    reject_secret_like_entry(normalized)
    return normalized


class MemoryStore:
    """Schema-versioned local JSON memory store."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else MEMORY_FILE
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self._lock = threading.RLock()
        self._entries: dict[str, MemoryEntry] = {}
        self._updated_at = _now_utc()
        self._loaded = False

    def load(self) -> dict[str, Any]:
        """Load the store, recovering from a valid backup when possible."""

        with self._lock:
            if not self.path.exists():
                self._entries = {}
                self._updated_at = _now_utc()
                self._loaded = True
                return self.export_snapshot()
            try:
                snapshot = self._read_snapshot(self.path)
                self._apply_snapshot(snapshot)
            except Exception:
                snapshot = self._recover_from_corruption()
                self._apply_snapshot(snapshot)
            self._loaded = True
            return self.export_snapshot()

    def save(self) -> None:
        """Persist the current snapshot atomically and refresh the backup."""

        with self._lock:
            self._ensure_loaded()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._updated_at = _now_utc()
            snapshot = self.export_snapshot()
            self._write_json_atomic(self.path, snapshot)
            try:
                self._write_json_atomic(self.backup_path, snapshot)
            except OSError:
                pass

    def list_entries(
        self,
        *,
        types: set[str] | list[str] | tuple[str, ...] | None = None,
        categories: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            type_filter = {str(t).strip().lower() for t in types or []}
            category_filter = {str(c).strip().lower() for c in categories or []}
            entries = []
            for entry in self._entries.values():
                if type_filter and entry.type not in type_filter:
                    continue
                if category_filter and entry.category not in category_filter:
                    continue
                entries.append(entry.to_dict())
            return entries

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_loaded()
            entry = self._entries.get(_strip_text(entry_id))
            return entry.to_dict() if entry else None

    def upsert_entry(
        self,
        entry: MemoryEntry | dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            raw: dict[str, Any] = {}
            if entry is not None:
                raw = (
                    entry.to_dict()
                    if isinstance(entry, MemoryEntry)
                    else copy.deepcopy(entry)
                )
                if not isinstance(raw, dict):
                    raise MemoryValidationError("memory entry must be an object")
            raw.update(fields)
            entry_id = _strip_text(raw.get("id"))
            existing = self._entries.get(entry_id) if entry_id else None
            normalized = validate_entry(raw, existing=existing)
            self._entries[normalized.id] = normalized
            self.save()
            return normalized.to_dict()

    def delete_entry(self, entry_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            normalized_id = _strip_text(entry_id)
            if normalized_id not in self._entries:
                return False
            del self._entries[normalized_id]
            self.save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._ensure_loaded()
            self._entries.clear()
            self.save()

    def search(
        self,
        query: str,
        limit: int = 10,
        *,
        types: set[str] | list[str] | tuple[str, ...] | None = None,
        categories: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_loaded()
            limit = max(0, int(limit or 0))
            if limit == 0:
                return []
            tokens = [
                t for t in re.split(r"\s+", _strip_text(query).lower()) if t
            ]
            candidates = self.list_entries(types=types, categories=categories)
            if not tokens:
                return candidates[:limit]

            scored: list[tuple[int, str, dict[str, Any]]] = []
            for entry in candidates:
                haystack = self._entry_search_text(entry)
                score = 0
                for token in tokens:
                    if entry["key"] == token:
                        score += 20
                    if token in entry["key"]:
                        score += 10
                    if token in entry["title"].lower():
                        score += 8
                    if token in haystack:
                        score += 2
                if score:
                    scored.append((score, entry.get("updated_at", ""), entry))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return [entry for _, _, entry in scored[:limit]]

    def mark_used(self, entry_id: str) -> bool:
        with self._lock:
            self._ensure_loaded()
            entry = self._entries.get(_strip_text(entry_id))
            if not entry:
                return False
            entry.last_used_at = _now_utc()
            self.save()
            return True

    def export_snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            return {
                "schema_version": SCHEMA_VERSION,
                "updated_at": self._updated_at,
                "entries": [entry.to_dict() for entry in self._entries.values()],
            }

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._updated_at = _strip_text(snapshot.get("updated_at")) or _now_utc()
        entries: dict[str, MemoryEntry] = {}
        for raw_entry in snapshot.get("entries", []):
            normalized = validate_entry(raw_entry, touch_updated=False)
            entries[normalized.id] = normalized
        self._entries = entries

    def _read_snapshot(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8", errors="strict") as fh:
            data = json.load(fh)
        return self._normalize_snapshot(data)

    def _normalize_snapshot(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise MemoryValidationError("memory snapshot must be an object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise MemoryValidationError(
                f"unsupported memory schema_version: {data.get('schema_version')!r}"
            )
        entries = data.get("entries")
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise MemoryValidationError("memory entries must be a list")
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _strip_text(data.get("updated_at")) or _now_utc(),
            "entries": entries,
        }

    def _recover_from_corruption(self) -> dict[str, Any]:
        try:
            backup = self._read_snapshot(self.backup_path)
            for raw_entry in backup.get("entries", []):
                validate_entry(raw_entry, touch_updated=False)
        except Exception:
            self._move_corrupt_live()
            return {
                "schema_version": SCHEMA_VERSION,
                "updated_at": _now_utc(),
                "entries": [],
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(self.path, backup)
        return backup

    def _move_corrupt_live(self) -> None:
        if not self.path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.stem}.corrupt.{stamp}{self.path.suffix}")
        while target.exists():
            target = self.path.with_name(
                f"{self.path.stem}.corrupt.{stamp}.{uuid.uuid4().hex[:8]}"
                f"{self.path.suffix}"
            )
        try:
            self.path.replace(target)
        except OSError:
            pass

    def _write_json_atomic(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{self.path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            if sys.platform != "win32":
                try:
                    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            os.replace(tmp, path)
            if sys.platform != "win32":
                try:
                    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _entry_search_text(entry: dict[str, Any]) -> str:
        parts = [
            entry.get("key", ""),
            entry.get("title", ""),
            entry.get("type", ""),
            entry.get("category", ""),
            " ".join(entry.get("tags") or []),
        ]
        try:
            parts.append(json.dumps(entry.get("value"), ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(entry.get("value")))
        return " ".join(str(p).lower() for p in parts)


_default_store: MemoryStore | None = None


def _store() -> MemoryStore:
    global _default_store
    if _default_store is None:
        _default_store = MemoryStore()
    return _default_store


def load() -> dict[str, Any]:
    return _store().load()


def save() -> None:
    _store().save()


def list_entries(**kwargs: Any) -> list[dict[str, Any]]:
    return _store().list_entries(**kwargs)


def get_entry(entry_id: str) -> dict[str, Any] | None:
    return _store().get_entry(entry_id)


def upsert_entry(
    entry: MemoryEntry | dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return _store().upsert_entry(entry, **fields)


def delete_entry(entry_id: str) -> bool:
    return _store().delete_entry(entry_id)


def clear() -> None:
    _store().clear()


def search(query: str, limit: int = 10, **kwargs: Any) -> list[dict[str, Any]]:
    return _store().search(query, limit=limit, **kwargs)


def mark_used(entry_id: str) -> bool:
    return _store().mark_used(entry_id)


def export_snapshot() -> dict[str, Any]:
    return _store().export_snapshot()
