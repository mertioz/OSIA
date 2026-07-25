"""Private, bounded local document artifacts (WOR-175).

Only opaque metadata is safe to persist in Autonomous checkpoints.  Extracted
text lives in this owner-local store and is exposed to the agent solely through
bounded, capability-scoped chunk reads.  Payloads are deliberately not
encrypted: OSIA has no local at-rest encryption/key-storage facility suitable
for arbitrary documents.  The residual risk is therefore local-device access;
short retention, restrictive permissions, integrity checks and deterministic
cleanup bound that risk without inventing weak custom cryptography.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable

from osia.config import CONFIG_DIR as _CONFIG_DIR
from osia.config import PRIVATE_DATA_DIR as _PRIVATE_DATA_DIR


ARTIFACT_ROOT = Path(_PRIVATE_DATA_DIR) / "document_artifacts"
ARTIFACT_TTL_SECONDS = 72 * 60 * 60
TEMP_TTL_SECONDS = 60 * 60
MAX_ARTIFACTS = 24
MAX_TOTAL_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_CONTENT_CHARS = 2_000_000
MAX_METADATA_BYTES = 16 * 1024
MAX_ARTIFACT_REFS = 16
DEFAULT_CHUNK_CHARS = 4_000
MAX_CHUNK_CHARS = 8_000

_ARTIFACT_RE = re.compile(r"^doc_[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PAGE_METHODS = frozenset({"native", "ocr"})
_DOCUMENT_TYPES = frozenset({"pdf", "png", "jpeg"})
_PROCESS_OWNER = secrets.token_hex(32)
try:
    import psutil

    _PROCESS_STARTED_AT = round(psutil.Process(os.getpid()).create_time(), 3)
except Exception:
    _PROCESS_STARTED_AT = 0.0

_owner_hash_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "osia_document_owner_hash", default=None,
)
_allowed_ids_var: contextvars.ContextVar[set[str] | None] = (
    contextvars.ContextVar("osia_document_allowed_ids", default=None)
)
_capture_var: contextvars.ContextVar[Callable[[dict], None] | None] = (
    contextvars.ContextVar("osia_document_capture", default=None)
)


class ArtifactStoreError(RuntimeError):
    """One stable, path-free artifact failure category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def valid_artifact_id(value) -> str:
    artifact_id = str(value or "")
    if not _ARTIFACT_RE.fullmatch(artifact_id):
        raise ArtifactStoreError("invalid_artifact_id")
    return artifact_id


def _owner_hash(owner_id: str) -> str:
    return hashlib.sha256(
        str(owner_id).encode("utf-8", errors="replace")
    ).hexdigest()


def current_owner_hash() -> str:
    return _owner_hash_var.get() or _owner_hash(_PROCESS_OWNER)


@contextmanager
def artifact_access_scope(
    owner_id: str,
    artifact_ids: Iterable[str] = (),
    capture_callback: Callable[[dict], None] | None = None,
):
    """Authorize one agent run without exposing its owner token to tools."""

    clean_ids = {
        artifact_id for artifact_id in artifact_ids
        if _ARTIFACT_RE.fullmatch(str(artifact_id or ""))
    }
    owner_token = _owner_hash_var.set(_owner_hash(owner_id))
    allowed_token = _allowed_ids_var.set(clean_ids)
    capture_token = _capture_var.set(capture_callback)
    try:
        yield
    finally:
        _capture_var.reset(capture_token)
        _allowed_ids_var.reset(allowed_token)
        _owner_hash_var.reset(owner_token)


def has_artifact_access_scope() -> bool:
    """Return whether the current call already has a document capability."""

    return _owner_hash_var.get() is not None


def capture_reference(reference: dict) -> None:
    """Send a sanitized reference to the active Autonomous run, if any."""

    clean = sanitize_reference(reference)
    if clean is None:
        return
    allowed = _allowed_ids_var.get()
    if allowed is not None:
        allowed.add(clean["artifact_id"])
    callback = _capture_var.get()
    if callback is not None:
        try:
            callback(dict(clean))
        except Exception:
            # The tool contract remains deterministic even if a caller-owned
            # checkpoint callback fails; Autonomous reports its own save error.
            pass


def sanitize_reference(value) -> dict | None:
    """Return the bounded checkpoint-safe artifact projection."""

    if not isinstance(value, dict):
        return None
    try:
        artifact_id = valid_artifact_id(value.get("artifact_id"))
        document_type = str(value.get("document_type") or "")
        if document_type not in _DOCUMENT_TYPES:
            return None
        page_count = int(value.get("page_count", 0))
        content_length = int(value.get("content_length", 0))
        content_hash = str(value.get("content_hash") or "")
        if not (1 <= page_count <= 100):
            return None
        if not (0 <= content_length <= MAX_CONTENT_CHARS):
            return None
        if not _HASH_RE.fullmatch(content_hash):
            return None
        raw_methods = value.get("page_methods") or []
        if not isinstance(raw_methods, list) or len(raw_methods) != page_count:
            return None
        page_methods = [str(item) for item in raw_methods]
        if any(item not in _PAGE_METHODS for item in page_methods):
            return None
        warnings = []
        for item in value.get("warnings") or []:
            code = str(item or "")
            if _SAFE_CODE_RE.fullmatch(code) and code not in warnings:
                warnings.append(code)
            if len(warnings) >= 8:
                break
        confidence = value.get("confidence")
        if confidence is not None:
            confidence = round(float(confidence), 4)
            if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
                confidence = None
        expires_at = int(float(value.get("expires_at", 0)))
    except (ArtifactStoreError, TypeError, ValueError, OverflowError):
        return None
    return {
        "artifact_id": artifact_id,
        "document_type": document_type,
        "page_count": page_count,
        "page_methods": page_methods,
        "content_length": content_length,
        "content_hash": content_hash,
        "confidence": confidence,
        "truncated": value.get("truncated") is True,
        "warnings": warnings,
        "resumable": value.get("resumable") is True,
        "expires_at": expires_at,
        "provenance": "local_document",
    }


def normalize_references(values) -> list[dict]:
    """Deduplicate and cap a checkpoint's artifact metadata."""

    out: list[dict] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else ():
        clean = sanitize_reference(value)
        if clean is None or clean["artifact_id"] in seen:
            continue
        seen.add(clean["artifact_id"])
        out.append(clean)
        if len(out) >= MAX_ARTIFACT_REFS:
            break
    return out


def referenced_ids(state: dict | None) -> set[str]:
    ids = {
        ref["artifact_id"]
        for ref in normalize_references(
            state.get("artifacts") if isinstance(state, dict) else []
        )
    }
    if isinstance(state, dict):
        for phase in state.get("phases") or []:
            if not isinstance(phase, dict):
                continue
            for task in phase.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                for artifact_id in task.get("artifact_ids") or []:
                    if _ARTIFACT_RE.fullmatch(str(artifact_id or "")):
                        ids.add(str(artifact_id))
    return ids


class ArtifactStore:
    """Atomic owner-local payload store with integrity and lifecycle bounds."""

    def __init__(self, root: Path = ARTIFACT_ROOT, *, clock=None):
        self.root = Path(root)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._live_ids: set[str] = set()

    @staticmethod
    def _chmod_private(path: Path, *, directory: bool = False) -> None:
        try:
            os.chmod(
                path,
                stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR,
            )
        except OSError:
            # Windows ACLs remain user-profile scoped; chmod is best effort.
            pass

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._chmod_private(self.root, directory=True)

    def _artifact_dir(self, artifact_id: str) -> Path:
        return self.root / valid_artifact_id(artifact_id)

    def _safe_remove_dir(self, directory: Path) -> bool:
        try:
            root = self.root.resolve(strict=False)
            target = directory.resolve(strict=False)
            if target.parent != root:
                return False
            name = target.name
            if not (_ARTIFACT_RE.fullmatch(name) or name.startswith(".doc_")):
                return False
            shutil.rmtree(target)
            return True
        except OSError:
            return False

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        ArtifactStore._chmod_private(path)

    @staticmethod
    def _read_bounded(path: Path, limit: int) -> bytes:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ArtifactStoreError("corrupted_artifact")
        return data

    def _load_metadata(self, artifact_id: str) -> tuple[Path, dict]:
        directory = self._artifact_dir(artifact_id)
        try:
            raw = self._read_bounded(
                directory / "metadata.json", MAX_METADATA_BYTES,
            )
            metadata = json.loads(raw.decode("utf-8"))
        except ArtifactStoreError:
            raise
        except FileNotFoundError as exc:
            raise ArtifactStoreError("missing_artifact") from exc
        except OSError as exc:
            raise ArtifactStoreError("unreadable_artifact") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreError("corrupted_artifact") from exc
        if not isinstance(metadata, dict):
            raise ArtifactStoreError("corrupted_artifact")
        reference = sanitize_reference(metadata)
        if reference is None or reference["artifact_id"] != artifact_id:
            raise ArtifactStoreError("corrupted_artifact")
        owner_hash = str(metadata.get("owner_hash") or "")
        if not _HASH_RE.fullmatch(owner_hash):
            raise ArtifactStoreError("corrupted_artifact")
        allowed = _allowed_ids_var.get()
        if metadata.get("owner_hash") != current_owner_hash():
            raise ArtifactStoreError("unauthorized_artifact")
        if allowed is not None and artifact_id not in allowed:
            raise ArtifactStoreError("unauthorized_artifact")
        try:
            if self._clock() >= float(metadata.get("expires_at", 0)):
                raise ArtifactStoreError("expired_artifact")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtifactStoreError("corrupted_artifact") from exc
        return directory, metadata

    def _read_payload(self, directory: Path, metadata: dict) -> str:
        try:
            payload = self._read_bounded(
                directory / "payload.txt", MAX_PAYLOAD_BYTES,
            )
        except FileNotFoundError as exc:
            raise ArtifactStoreError("missing_artifact") from exc
        except OSError as exc:
            raise ArtifactStoreError("unreadable_artifact") from exc
        if len(payload) != int(metadata.get("payload_bytes", -1)):
            raise ArtifactStoreError("corrupted_artifact")
        if hashlib.sha256(payload).hexdigest() != metadata.get("content_hash"):
            raise ArtifactStoreError("corrupted_artifact")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactStoreError("corrupted_artifact") from exc
        if len(text) != int(metadata.get("content_length", -1)):
            raise ArtifactStoreError("corrupted_artifact")
        return text

    def create(self, content: str, metadata: dict) -> dict:
        if not isinstance(content, str):
            raise ArtifactStoreError("extraction_failure")
        payload = content.encode("utf-8")
        if len(content) > MAX_CONTENT_CHARS or len(payload) > MAX_PAYLOAD_BYTES:
            raise ArtifactStoreError("limit_exceeded")
        with self._lock:
            self._ensure_root()
            self.cleanup()
            allowed = _allowed_ids_var.get()
            if allowed is not None and len(allowed) >= MAX_ARTIFACT_REFS:
                raise ArtifactStoreError("limit_exceeded")
            artifact_dirs = [
                item for item in self.root.iterdir()
                if item.is_dir() and _ARTIFACT_RE.fullmatch(item.name)
            ]
            if len(artifact_dirs) >= MAX_ARTIFACTS:
                raise ArtifactStoreError("limit_exceeded")
            total = 0
            for item in artifact_dirs:
                try:
                    total += (item / "payload.txt").stat().st_size
                except OSError:
                    continue
            if total + len(payload) > MAX_TOTAL_PAYLOAD_BYTES:
                raise ArtifactStoreError("limit_exceeded")

            artifact_id = f"doc_{secrets.token_hex(16)}"
            while (self.root / artifact_id).exists():
                artifact_id = f"doc_{secrets.token_hex(16)}"
            created_at = int(self._clock())
            candidate = {
                **dict(metadata or {}),
                "artifact_id": artifact_id,
                "content_length": len(content),
                "content_hash": hashlib.sha256(payload).hexdigest(),
                "truncated": False,
                "resumable": True,
                "expires_at": created_at + ARTIFACT_TTL_SECONDS,
                "provenance": "local_document",
            }
            reference = sanitize_reference(candidate)
            if reference is None:
                raise ArtifactStoreError("extraction_failure")
            stored = {
                **reference,
                "schema_version": 1,
                "owner_hash": current_owner_hash(),
                "payload_bytes": len(payload),
                "created_at": created_at,
                "creator_pid": os.getpid(),
                "creator_started_at": _PROCESS_STARTED_AT,
            }
            encoded_metadata = json.dumps(
                stored, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded_metadata) > MAX_METADATA_BYTES:
                raise ArtifactStoreError("limit_exceeded")

            temporary = self.root / f".{artifact_id}.{secrets.token_hex(8)}.tmp"
            final = self.root / artifact_id
            try:
                temporary.mkdir()
                self._chmod_private(temporary, directory=True)
                self._write_private(temporary / "payload.txt", payload)
                self._write_private(temporary / "metadata.json", encoded_metadata)
                os.replace(temporary, final)
                self._chmod_private(final, directory=True)
            except Exception:
                self._safe_remove_dir(temporary)
                raise
            self._live_ids.add(artifact_id)
            allowed = _allowed_ids_var.get()
            if allowed is not None:
                allowed.add(artifact_id)
            return reference

    def inspect(self, artifact_id: str) -> dict:
        with self._lock:
            directory, metadata = self._load_metadata(artifact_id)
            self._read_payload(directory, metadata)
            self._live_ids.add(artifact_id)
            reference = sanitize_reference(metadata)
            if reference is None:
                raise ArtifactStoreError("corrupted_artifact")
            return reference

    def read_chunk(self, artifact_id: str, offset=0, chunk_chars=None) -> dict:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ArtifactStoreError("invalid_offset")
        if chunk_chars is None:
            chunk_chars = DEFAULT_CHUNK_CHARS
        if (isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int)
                or chunk_chars < 1 or chunk_chars > MAX_CHUNK_CHARS):
            raise ArtifactStoreError("invalid_chunk_size")
        with self._lock:
            directory, metadata = self._load_metadata(artifact_id)
            text = self._read_payload(directory, metadata)
            if offset > len(text):
                raise ArtifactStoreError("invalid_offset")
            chunk = text[offset: offset + chunk_chars]
            next_offset = offset + len(chunk)
            at_end = next_offset >= len(text)
            self._live_ids.add(str(artifact_id))
            return {
                "ok": True,
                "artifact_id": str(artifact_id),
                "offset": offset,
                "next_offset": next_offset,
                "content_length": len(text),
                "content": chunk,
                "end_of_content": at_end,
                "eof_marker": "END_OF_DOCUMENT" if at_end else None,
            }

    def delete(self, artifact_id: str) -> bool:
        with self._lock:
            try:
                directory = self._artifact_dir(artifact_id)
            except ArtifactStoreError:
                return False
            self._live_ids.discard(str(artifact_id))
            return self._safe_remove_dir(directory) if directory.exists() else False

    def cleanup(self, protected_ids: Iterable[str] = (), *,
                remove_unreferenced: bool = False) -> dict:
        """Delete stale temp/orphan payloads, never a resumable reference."""

        with self._lock:
            self._ensure_root()
            protected = {
                str(item) for item in protected_ids
                if _ARTIFACT_RE.fullmatch(str(item or ""))
            }
            protected.update(self._live_ids)
            removed = 0
            now = self._clock()
            for item in list(self.root.iterdir()):
                if not item.is_dir():
                    continue
                if item.name.startswith(".doc_"):
                    try:
                        stale = now - item.stat().st_mtime >= TEMP_TTL_SECONDS
                    except OSError:
                        stale = True
                    if stale and self._safe_remove_dir(item):
                        removed += 1
                    continue
                if not _ARTIFACT_RE.fullmatch(item.name) or item.name in protected:
                    continue
                try:
                    raw = self._read_bounded(
                        item / "metadata.json", MAX_METADATA_BYTES,
                    )
                    metadata = json.loads(raw.decode("utf-8"))
                    expired = now >= float(metadata.get("expires_at", 0))
                except Exception:
                    metadata = {}
                    expired = True
                orphaned = (
                    remove_unreferenced and not self._creator_alive(metadata)
                )
                if (expired or orphaned) and self._safe_remove_dir(item):
                    removed += 1
            return {"removed": removed}

    @staticmethod
    def _creator_alive(metadata: dict) -> bool:
        try:
            import psutil

            pid = int(metadata.get("creator_pid", 0))
            started_at = float(metadata.get("creator_started_at", 0))
            if pid <= 0 or started_at <= 0:
                return False
            process = psutil.Process(pid)
            return abs(process.create_time() - started_at) < 0.01
        except Exception:
            return False


_STORE = ArtifactStore()


def get_store() -> ArtifactStore:
    return _STORE


def set_store_for_testing(store: ArtifactStore):
    """Swap the process store for deterministic tests; returns the old store."""

    global _STORE
    previous = _STORE
    _STORE = store
    return previous


def _checkpoint_references() -> set[str]:
    ids: set[str] = set()
    config_dir = Path(_CONFIG_DIR)
    for name in ("task_state.json", "task_state_mobile.json"):
        path = config_dir / name
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            loaded = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(loaded, dict):
            resumable_failure = (
                loaded.get("status") == "failed"
                and isinstance(loaded.get("failure_diagnostic"), dict)
                and loaded["failure_diagnostic"].get("resumable") is True
            )
            if loaded.get("status") == "in_progress" or resumable_failure:
                ids.update(referenced_ids(loaded))
    return ids


def cleanup_startup() -> dict:
    return get_store().cleanup(
        _checkpoint_references(), remove_unreferenced=True,
    )


def cleanup_state_artifacts(state: dict, *, clear_references: bool = True) -> int:
    ids = referenced_ids(state)
    removed = sum(1 for artifact_id in ids if get_store().delete(artifact_id))
    if clear_references and isinstance(state, dict):
        state["artifacts"] = []
        for phase in state.get("phases") or []:
            if not isinstance(phase, dict):
                continue
            for task in phase.get("tasks") or []:
                if isinstance(task, dict):
                    task["artifact_ids"] = []
    return removed


if not any(
    argument in {"--worker", "--osia-document-worker"}
    for argument in sys.argv[1:]
):
    try:
        cleanup_startup()
    except Exception:
        # Startup cleanup is best effort and deliberately silent: no paths or
        # payload details may enter logs during recovery.
        pass


__all__ = [
    "ARTIFACT_ROOT",
    "ArtifactStore",
    "ArtifactStoreError",
    "artifact_access_scope",
    "capture_reference",
    "cleanup_startup",
    "cleanup_state_artifacts",
    "get_store",
    "has_artifact_access_scope",
    "normalize_references",
    "referenced_ids",
    "sanitize_reference",
    "set_store_for_testing",
    "valid_artifact_id",
]
