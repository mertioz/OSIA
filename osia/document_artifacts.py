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
MAX_PRIVATE_METADATA_BYTES = 64 * 1024
MAX_ARTIFACT_REFS = 16
DEFAULT_CHUNK_CHARS = 4_000
MAX_CHUNK_CHARS = 8_000

_ARTIFACT_RE = re.compile(r"^doc_[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PAGE_METHODS = frozenset({"native", "ocr"})
_DOCUMENT_TYPES = frozenset({
    "pdf", "png", "jpeg", "webp", "gif", "tiff", "bmp",
    "wav", "mp3", "m4a", "aac", "ogg", "flac", "mp4", "mov", "webm", "mkv",
    "docx", "odt", "rtf", "txt", "markdown",
    "html", "epub", "xlsx", "ods", "csv", "tsv", "pptx", "odp",
})
_DOCUMENT_FAMILIES = frozenset({
    "document", "spreadsheet", "presentation", "image", "audio", "video",
})
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
        warnings = []
        for item in value.get("warnings") or []:
            code = str(item or "")
            if _SAFE_CODE_RE.fullmatch(code) and code not in warnings:
                warnings.append(code)
            if len(warnings) >= 8:
                break
        expires_at = int(float(value.get("expires_at", 0)))
    except (ArtifactStoreError, TypeError, ValueError, OverflowError):
        return None
    schema_version = int(value.get("schema_version", 1) or 1)
    if schema_version == 2:
        try:
            format_label = str(value.get("format_label") or value.get("document_type") or "")
            family = str(value.get("document_family") or value.get("family") or "")
            status = str(value.get("status") or "")
            if format_label not in _DOCUMENT_TYPES or family not in _DOCUMENT_FAMILIES:
                return None
            if status not in {"complete", "incomplete"}:
                return None
            counts = {}
            for name in (
                "page_count", "frame_count", "track_count", "segment_count",
                "chapter_count", "section_count", "table_count", "sheet_count",
                "slide_count", "chunk_count", "embedded_count",
            ):
                count = int(value.get(name, 0) or 0)
                if count < 0 or count > 2_000_000:
                    return None
                counts[name] = count
            operations = []
            for operation in value.get("supported_queries") or []:
                code = str(operation or "")
                if _SAFE_CODE_RE.fullmatch(code) and code not in operations:
                    operations.append(code)
                if len(operations) >= 32:
                    break
            page_methods = [
                str(item) for item in (value.get("page_methods") or [])
                if str(item) in _PAGE_METHODS
            ][:100]
            content_length = max(0, min(
                int(value.get("content_length", 0) or 0), MAX_CONTENT_CHARS,
            ))
            confidence = value.get("confidence")
            if confidence is not None:
                confidence = round(float(confidence), 4)
                if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
                    confidence = None
            duration = value.get("duration_seconds")
            if duration is not None:
                duration = round(float(duration), 3)
                if not math.isfinite(duration) or duration < 0 or duration > 3_600:
                    duration = None
            width = max(0, min(int(value.get("width", 0) or 0), 8_192))
            height = max(0, min(int(value.get("height", 0) or 0), 8_192))
        except (TypeError, ValueError, OverflowError):
            return None
        return {
            "schema_version": 2,
            "artifact_id": artifact_id,
            "document_id": artifact_id,
            "document_type": format_label,
            "format_label": format_label,
            "document_family": family,
            "status": status,
            **counts,
            "page_methods": page_methods,
            "content_length": content_length,
            "confidence": confidence,
            "duration_seconds": duration,
            "width": width,
            "height": height,
            "provenance_available": value.get("provenance_available") is True,
            "truncated": value.get("truncated") is True,
            "warnings": warnings,
            "supported_queries": operations,
            "resumable": value.get("resumable") is True,
            "expires_at": expires_at,
            "provenance": "local_document",
        }

    # Read-compatible public projection for unexpired WOR-175 v1 artifacts.
    try:
        document_type = str(value.get("document_type") or "")
        page_count = int(value.get("page_count", 0))
        content_length = int(value.get("content_length", 0))
        raw_methods = value.get("page_methods") or []
        if document_type not in {"pdf", "png", "jpeg"}:
            return None
        if not (1 <= page_count <= 100) or not (0 <= content_length <= MAX_CONTENT_CHARS):
            return None
        if not isinstance(raw_methods, list) or len(raw_methods) != page_count:
            return None
        page_methods = [str(item) for item in raw_methods]
        if any(item not in _PAGE_METHODS for item in page_methods):
            return None
        confidence = value.get("confidence")
        if confidence is not None:
            confidence = round(float(confidence), 4)
            if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
                confidence = None
    except (TypeError, ValueError, OverflowError):
        return None
    return {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "document_id": artifact_id,
        "document_type": document_type,
        "format_label": document_type,
        "document_family": "image" if document_type in {"png", "jpeg"} else "document",
        "status": "complete",
        "page_count": page_count,
        "section_count": 0,
        "table_count": 0,
        "sheet_count": 0,
        "slide_count": 0,
        "chunk_count": max(1, math.ceil(content_length / DEFAULT_CHUNK_CHARS)),
        "embedded_count": 0,
        "page_methods": page_methods,
        "content_length": content_length,
        "confidence": confidence,
        "provenance_available": True,
        "truncated": value.get("truncated") is True,
        "warnings": warnings,
        "supported_queries": ["summary", "search", "chunks", "provenance"],
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
            if (directory / "public.json").is_file():
                public_raw = self._read_bounded(
                    directory / "public.json", MAX_METADATA_BYTES,
                )
                private_raw = self._read_bounded(
                    directory / "private.json", MAX_PRIVATE_METADATA_BYTES,
                )
                public = json.loads(public_raw.decode("utf-8"))
                private = json.loads(private_raw.decode("utf-8"))
                if not isinstance(public, dict) or not isinstance(private, dict):
                    raise ArtifactStoreError("corrupted_artifact")
                metadata = {**private, **public}
                metadata["_public"] = public
                metadata["_private"] = private
            else:
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
        if int(metadata.get("schema_version", 1) or 1) == 2:
            document = self._read_structured_payload(directory, metadata)
            return "\n".join(
                str(chunk.get("text") or "")
                for chunk in document.get("chunks") or []
            )
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

    def _read_structured_payload(self, directory: Path, metadata: dict) -> dict:
        try:
            payload = self._read_bounded(
                directory / "payload.json", MAX_PAYLOAD_BYTES,
            )
        except FileNotFoundError as exc:
            raise ArtifactStoreError("missing_artifact") from exc
        except OSError as exc:
            raise ArtifactStoreError("unreadable_artifact") from exc
        private = metadata.get("_private") if isinstance(metadata.get("_private"), dict) else metadata
        if len(payload) != int(private.get("payload_bytes", -1)):
            raise ArtifactStoreError("corrupted_artifact")
        if hashlib.sha256(payload).hexdigest() != private.get("payload_hash"):
            raise ArtifactStoreError("corrupted_artifact")
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreError("corrupted_artifact") from exc
        if not isinstance(document, dict) or document.get("schema_version") != 2:
            raise ArtifactStoreError("corrupted_artifact")
        if document.get("format_label") != metadata.get("format_label"):
            raise ArtifactStoreError("corrupted_artifact")
        return document

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
                    payload_path = (
                        item / "payload.json"
                        if (item / "payload.json").is_file()
                        else item / "payload.txt"
                    )
                    total += payload_path.stat().st_size
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
                "content_hash": hashlib.sha256(payload).hexdigest(),
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

    def create_v2(self, document: dict, private_metadata: dict) -> dict:
        """Atomically create or deduplicate one structured artifact."""

        from osia.document_models import (
            SCHEMA_VERSION,
            SUPPORTED_QUERY_OPERATIONS,
            document_counts,
        )

        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise ArtifactStoreError("extraction_failure")
        try:
            payload = json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtifactStoreError("extraction_failure") from exc
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ArtifactStoreError("limit_exceeded")
        source_hash = str((private_metadata or {}).get("source_hash") or "")
        source_identity = str(
            (private_metadata or {}).get("source_identity") or ""
        )[:128]
        parser_version = str((private_metadata or {}).get("parser_version") or "")
        if (
            not _HASH_RE.fullmatch(source_hash)
            or not source_identity
            or not _SAFE_CODE_RE.fullmatch(parser_version)
        ):
            raise ArtifactStoreError("extraction_failure")
        owner = current_owner_hash()
        with self._lock:
            self._ensure_root()
            self.cleanup()
            allowed = _allowed_ids_var.get()
            # Dedupe only inside this exact owner/policy/parser scope and only
            # after full payload integrity validation.
            for item in sorted(self.root.iterdir(), key=lambda path: path.name):
                if not item.is_dir() or not _ARTIFACT_RE.fullmatch(item.name):
                    continue
                try:
                    public_raw = self._read_bounded(item / "public.json", MAX_METADATA_BYTES)
                    private_raw = self._read_bounded(item / "private.json", MAX_PRIVATE_METADATA_BYTES)
                    public = json.loads(public_raw.decode("utf-8"))
                    private = json.loads(private_raw.decode("utf-8"))
                    if not (
                        private.get("owner_hash") == owner
                        and private.get("source_hash") == source_hash
                        and private.get("source_identity") == source_identity
                        and private.get("parser_version") == parser_version
                        and int(private.get("policy_epoch", 0) or 0)
                            == int((private_metadata or {}).get("policy_epoch", 0) or 0)
                        and public.get("status") == "complete"
                        and self._clock() < float(public.get("expires_at", 0))
                    ):
                        continue
                    combined = {**private, **public, "_private": private, "_public": public}
                    self._read_structured_payload(item, combined)
                    reference = sanitize_reference(public)
                    if reference is None:
                        continue
                    if allowed is not None:
                        allowed.add(reference["artifact_id"])
                    self._live_ids.add(reference["artifact_id"])
                    return reference
                except Exception:
                    continue
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
                    payload_path = item / "payload.json" if (item / "payload.json").is_file() else item / "payload.txt"
                    total += payload_path.stat().st_size
                except OSError:
                    continue
            if total + len(payload) > MAX_TOTAL_PAYLOAD_BYTES:
                raise ArtifactStoreError("limit_exceeded")
            artifact_id = f"doc_{secrets.token_hex(16)}"
            while (self.root / artifact_id).exists():
                artifact_id = f"doc_{secrets.token_hex(16)}"
            created_at = int(self._clock())
            requested_ttl = int((private_metadata or {}).get("ttl_seconds", ARTIFACT_TTL_SECONDS) or ARTIFACT_TTL_SECONDS)
            ttl = min(max(60, requested_ttl), 7 * 24 * 60 * 60)
            public = {
                "schema_version": 2,
                "artifact_id": artifact_id,
                "document_id": artifact_id,
                "format_label": str(document.get("format_label") or ""),
                "document_type": str(document.get("format_label") or ""),
                "document_family": str(document.get("family") or ""),
                "status": str(document.get("status") or "complete"),
                "warnings": list(document.get("warnings") or [])[:32],
                **document_counts(document),
                "page_methods": list((document.get("metadata") or {}).get("page_methods") or [])[:100],
                "content_length": len("\n".join(
                    str(chunk.get("text") or "")
                    for chunk in document.get("chunks") or []
                )),
                "confidence": (document.get("metadata") or {}).get("confidence"),
                "provenance_available": bool(
                    document.get("provenance")
                    or any(chunk.get("provenance") for chunk in document.get("chunks") or [])
                ),
                "supported_queries": list(SUPPORTED_QUERY_OPERATIONS),
                "truncated": False,
                "resumable": True,
                "expires_at": created_at + ttl,
                "provenance": "local_document",
            }
            metadata = document.get("metadata") or {}
            public["duration_seconds"] = metadata.get("duration_seconds")
            public["width"] = metadata.get("width")
            public["height"] = metadata.get("height")
            requested_queries = document.get("supported_queries")
            if isinstance(requested_queries, list):
                public["supported_queries"] = [
                    item for item in requested_queries
                    if item in SUPPORTED_QUERY_OPERATIONS
                ][:32]
            reference = sanitize_reference(public)
            if reference is None:
                raise ArtifactStoreError("extraction_failure")
            private = {
                "schema_version": 2,
                "artifact_id": artifact_id,
                "owner_hash": owner,
                "source_hash": source_hash,
                "source_identity": source_identity,
                "private_filename": str((private_metadata or {}).get("private_filename") or "")[:260],
                "storage_key": "payload.json",
                "payload_hash": hashlib.sha256(payload).hexdigest(),
                "payload_bytes": len(payload),
                "parser_version": parser_version,
                "dependency_versions": dict((private_metadata or {}).get("dependency_versions") or {}),
                "limits": dict((private_metadata or {}).get("limits") or {}),
                "original_metadata": dict(
                    (private_metadata or {}).get("original_metadata") or {}
                ),
                "policy_epoch": int((private_metadata or {}).get("policy_epoch", 0) or 0),
                "created_at": created_at,
                "creator_pid": os.getpid(),
                "creator_started_at": _PROCESS_STARTED_AT,
            }
            encoded_public = json.dumps(
                public, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            encoded_private = json.dumps(
                private, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded_public) > MAX_METADATA_BYTES or len(encoded_private) > MAX_PRIVATE_METADATA_BYTES:
                raise ArtifactStoreError("limit_exceeded")
            temporary = self.root / f".{artifact_id}.{secrets.token_hex(8)}.tmp"
            final = self.root / artifact_id
            try:
                temporary.mkdir()
                self._chmod_private(temporary, directory=True)
                self._write_private(temporary / "payload.json", payload)
                self._write_private(temporary / "public.json", encoded_public)
                self._write_private(temporary / "private.json", encoded_private)
                os.replace(temporary, final)
                self._chmod_private(final, directory=True)
            except Exception:
                self._safe_remove_dir(temporary)
                raise
            self._live_ids.add(artifact_id)
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

    @staticmethod
    def _query_cursor(operation: str, offset: int) -> str:
        return f"cursor_{operation}_{int(offset):08x}"

    @staticmethod
    def _parse_query_cursor(operation: str, cursor) -> int:
        if cursor in {None, ""}:
            return 0
        match = re.fullmatch(rf"cursor_{re.escape(operation)}_([0-9a-f]{{8}})", str(cursor))
        if not match:
            raise ArtifactStoreError("invalid_cursor")
        return int(match.group(1), 16)

    @staticmethod
    def _clip_query_value(value, remaining: list[int]):
        if isinstance(value, str):
            if remaining[0] <= 0:
                return ""
            result = value[:remaining[0]]
            remaining[0] -= len(result)
            return result
        if isinstance(value, list):
            return [ArtifactStore._clip_query_value(item, remaining) for item in value]
        if isinstance(value, dict):
            return {
                str(key): ArtifactStore._clip_query_value(item, remaining)
                for key, item in value.items()
            }
        return value

    def query(
        self,
        artifact_id: str,
        operation: str,
        *,
        query: str | None = None,
        cursor: str | None = None,
        max_items: int = 20,
        max_chars: int = MAX_CHUNK_CHARS,
        section_id: str | None = None,
        table_id: str | None = None,
        sheet_id: str | None = None,
        cell_range_ref: str | None = None,
        slide_id: str | None = None,
        embedded_artifact_id: str | None = None,
        frame_id: str | None = None,
        track_id: str | None = None,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> dict:
        """Execute one centrally bounded deterministic structured query."""

        if isinstance(max_items, bool) or not isinstance(max_items, int) or not (1 <= max_items <= 100):
            raise ArtifactStoreError("invalid_query_limit")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not (1 <= max_chars <= MAX_CHUNK_CHARS):
            raise ArtifactStoreError("invalid_query_limit")
        operation = str(operation or "")
        from osia.document_models import SUPPORTED_QUERY_OPERATIONS

        if operation not in SUPPORTED_QUERY_OPERATIONS:
            raise ArtifactStoreError("unsupported_query")
        with self._lock:
            directory, metadata = self._load_metadata(artifact_id)
            schema_version = int(metadata.get("schema_version", 1) or 1)
            if schema_version == 2:
                document = self._read_structured_payload(directory, metadata)
            else:
                text = self._read_payload(directory, metadata)
                document = {
                    "schema_version": 1,
                    "format_label": metadata.get("document_type"),
                    "family": "image" if metadata.get("document_type") in {"png", "jpeg"} else "document",
                    "status": "complete",
                    "warnings": list(metadata.get("warnings") or []),
                    "chunks": [
                        {
                            "chunk_id": f"chunk_{index // DEFAULT_CHUNK_CHARS + 1:06d}",
                            "ordinal": index // DEFAULT_CHUNK_CHARS,
                            "text": text[index:index + DEFAULT_CHUNK_CHARS],
                            "provenance": [],
                        }
                        for index in range(0, len(text), DEFAULT_CHUNK_CHARS)
                    ],
                    "sections": [], "headings": [], "paragraphs": [],
                    "tables": [], "sheets": [], "slides": [], "notes": [],
                    "embedded": [], "provenance": [],
                }
            offset = self._parse_query_cursor(operation, cursor)
            result: dict = {
                "ok": True,
                "artifact_id": str(artifact_id),
                "document_id": str(artifact_id),
                "operation": operation,
                "complete": True,
                "truncated": False,
                "not_found": False,
                "unsupported": False,
                "next_cursor": None,
            }
            items: list = []
            total = 0
            if operation == "summary":
                chunks = document.get("chunks") or []
                preview = "\n".join(str(item.get("text") or "") for item in chunks[:2])
                result["summary"] = {
                    "format_label": document.get("format_label"),
                    "document_family": document.get("family"),
                    "status": document.get("status"),
                    "warnings": list(document.get("warnings") or []),
                    "preview": preview,
                    "counts": {
                        "chunks": len(chunks),
                        "frames": len(document.get("frames") or []),
                        "tracks": len(document.get("tracks") or []),
                        "segments": len(document.get("transcript") or []),
                        "chapters": len(document.get("chapters") or []),
                        "sections": len(document.get("sections") or []),
                        "tables": len(document.get("tables") or []),
                        "sheets": len(document.get("sheets") or []),
                        "slides": len(document.get("slides") or []),
                        "embedded": len(document.get("embedded") or []),
                    },
                }
            elif operation == "search":
                needle = str(query or "")
                if not needle or len(needle) > 1_000:
                    raise ArtifactStoreError("invalid_query")
                matches = []
                folded_needle = needle.casefold()
                chunks = document.get("chunks") or []
                for chunk in chunks[offset:]:
                    text = str(chunk.get("text") or "")
                    position = text.casefold().find(folded_needle)
                    if position < 0:
                        continue
                    start = max(0, position - 160)
                    end = min(len(text), position + len(needle) + 320)
                    matches.append({
                        "chunk_id": chunk.get("chunk_id"),
                        "ordinal": chunk.get("ordinal"),
                        "snippet": text[start:end],
                        "provenance": chunk.get("provenance") or [],
                    })
                    if len(matches) >= max_items:
                        break
                items = matches
                total = len(chunks)
                consumed = total if not matches else int(matches[-1].get("ordinal", 0)) + 1
                if consumed < total:
                    result["truncated"] = True
                    result["complete"] = False
                    result["next_cursor"] = self._query_cursor(operation, consumed)
                result["not_found"] = not matches
            elif operation == "chunks":
                source = document.get("chunks") or []
                total = len(source)
                items = source[offset:offset + max_items]
            elif operation in {"sections", "headings"}:
                source = document.get(operation) or []
                if section_id and operation == "sections":
                    source = [item for item in source if item.get("section_id") == section_id]
                total = len(source)
                items = source[offset:offset + max_items]
                result["not_found"] = bool(section_id and not items)
            elif operation == "table":
                source = document.get("tables") or []
                if table_id:
                    source = [item for item in source if item.get("table_id") == table_id]
                total = len(source)
                items = source[offset:offset + min(max_items, 10)]
                result["not_found"] = bool(table_id and not items)
            elif operation == "sheets":
                source = [
                    {key: value for key, value in sheet.items() if key != "cells"}
                    for sheet in document.get("sheets") or []
                ]
                total = len(source)
                items = source[offset:offset + max_items]
                if not source:
                    result["unsupported"] = True
            elif operation == "range":
                if not sheet_id or not cell_range_ref:
                    raise ArtifactStoreError("invalid_query")
                sheet = next((item for item in document.get("sheets") or [] if item.get("sheet_id") == sheet_id), None)
                if sheet is None:
                    result["not_found"] = True
                    source = []
                else:
                    bounds = self._range_bounds(cell_range_ref)
                    source = [
                        cell for cell in sheet.get("cells") or []
                        if bounds[0] <= int(cell.get("row", 0)) <= bounds[2]
                        and bounds[1] <= int(cell.get("column", 0)) <= bounds[3]
                    ]
                total = len(source)
                items = source[offset:offset + max_items]
            elif operation == "formula":
                source = []
                for sheet in document.get("sheets") or []:
                    if sheet_id and sheet.get("sheet_id") != sheet_id:
                        continue
                    for cell in sheet.get("cells") or []:
                        if cell.get("formula") is not None or cell.get("formula_like") is True:
                            source.append({"sheet_id": sheet.get("sheet_id"), **cell})
                total = len(source)
                items = source[offset:offset + max_items]
                if not document.get("sheets"):
                    result["unsupported"] = True
            elif operation == "slides":
                source = document.get("slides") or []
                if slide_id:
                    source = [item for item in source if item.get("slide_id") == slide_id]
                total = len(source)
                items = source[offset:offset + max_items]
                result["not_found"] = bool(slide_id and not items)
                if not document.get("slides"):
                    result["unsupported"] = True
            elif operation == "notes":
                source = document.get("notes") or []
                if slide_id:
                    source = [item for item in source if item.get("slide_id") == slide_id]
                total = len(source)
                items = source[offset:offset + max_items]
                if not document.get("slides"):
                    result["unsupported"] = True
            elif operation == "embedded":
                source = document.get("embedded") or []
                if embedded_artifact_id:
                    source = [item for item in source if item.get("embedded_artifact_id") == embedded_artifact_id]
                total = len(source)
                items = source[offset:offset + max_items]
                result["not_found"] = bool(embedded_artifact_id and not items)
            elif operation == "provenance":
                source = document.get("provenance") or []
                if not source:
                    source = [
                        {"chunk_id": item.get("chunk_id"), "provenance": item.get("provenance") or []}
                        for item in document.get("chunks") or []
                    ]
                total = len(source)
                items = source[offset:offset + max_items]
            elif operation == "metadata":
                result["metadata"] = dict(document.get("metadata") or {})
            elif operation in {"frames", "pages"}:
                source = document.get("frames") or []
                if operation == "pages":
                    source = [item for item in source if item.get("kind") == "page"]
                if frame_id:
                    source = [item for item in source if item.get("frame_id") == frame_id]
                total = len(source)
                items = source[offset:offset + max_items]
                result["not_found"] = bool(frame_id and not items)
            elif operation == "ocr":
                source = document.get("ocr_blocks") or []
                if frame_id:
                    source = [item for item in source if item.get("frame_id") == frame_id]
                if query:
                    needle = str(query).casefold()
                    source = [
                        item for item in source
                        if needle in str(item.get("text") or "").casefold()
                    ]
                total = len(source)
                items = source[offset:offset + max_items]
                result["not_found"] = bool((frame_id or query) and not items)
            elif operation == "transcript":
                source = document.get("transcript") or []
                if track_id:
                    source = [item for item in source if item.get("track_id") == track_id]
                total = len(source)
                items = source[offset:offset + max_items]
            elif operation in {"time_range", "timeline"}:
                if start_seconds is None or end_seconds is None:
                    raise ArtifactStoreError("invalid_query")
                try:
                    start = float(start_seconds)
                    end = float(end_seconds)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ArtifactStoreError("invalid_query") from exc
                if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
                    raise ArtifactStoreError("invalid_query")
                duration = float((document.get("metadata") or {}).get("duration_seconds", 0) or 0)
                if end > min(3_600.0, duration + 0.001):
                    raise ArtifactStoreError("invalid_query")
                key = "timeline" if operation == "timeline" else "transcript"
                source = [
                    item for item in document.get(key) or []
                    if float(item.get("end_seconds", item.get("time_seconds", 0)) or 0) >= start
                    and float(item.get("start_seconds", item.get("time_seconds", 0)) or 0) <= end
                ]
                total = len(source)
                items = source[offset:offset + max_items]
            elif operation in {"tracks", "chapters"}:
                source = document.get(operation) or []
                if operation == "tracks" and track_id:
                    source = [item for item in source if item.get("track_id") == track_id]
                total = len(source)
                items = source[offset:offset + max_items]
                result["not_found"] = bool(track_id and not items)
            if operation != "summary":
                result["items"] = items
                if total and offset + len(items) < total and not result.get("next_cursor"):
                    result["truncated"] = True
                    result["complete"] = False
                    result["next_cursor"] = self._query_cursor(operation, offset + len(items))
            control_keys = {
                "ok", "artifact_id", "document_id", "operation", "complete",
                "truncated", "not_found", "unsupported", "next_cursor",
            }
            remaining = [max_chars]
            clipped = {
                key: value if key in control_keys else self._clip_query_value(value, remaining)
                for key, value in result.items()
            }
            # Clipping string fields is itself explicit truncation.
            if clipped != result:
                clipped["truncated"] = True
                clipped["complete"] = False
            self._live_ids.add(str(artifact_id))
            return clipped

    @staticmethod
    def _range_bounds(value: str) -> tuple[int, int, int, int]:
        coordinate_re = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")

        def parse(part: str) -> tuple[int, int]:
            match = coordinate_re.fullmatch(part)
            if not match:
                raise ArtifactStoreError("invalid_cell_range")
            column = 0
            for char in match.group(1).upper():
                column = column * 26 + ord(char) - 64
            row = int(match.group(2))
            if row > 1_048_576 or column > 16_384:
                raise ArtifactStoreError("invalid_cell_range")
            return row, column

        parts = str(value or "").split(":")
        if len(parts) == 1:
            start = end = parse(parts[0])
        elif len(parts) == 2:
            start, end = parse(parts[0]), parse(parts[1])
        else:
            raise ArtifactStoreError("invalid_cell_range")
        if start[0] > end[0] or start[1] > end[1]:
            raise ArtifactStoreError("invalid_cell_range")
        if (end[0] - start[0] + 1) * (end[1] - start[1] + 1) > 10_000:
            raise ArtifactStoreError("invalid_query_limit")
        return start[0], start[1], end[0], end[1]

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
                    if (item / "public.json").is_file():
                        public = json.loads(self._read_bounded(
                            item / "public.json", MAX_METADATA_BYTES,
                        ).decode("utf-8"))
                        private = json.loads(self._read_bounded(
                            item / "private.json", MAX_PRIVATE_METADATA_BYTES,
                        ).decode("utf-8"))
                        metadata = {**private, **public}
                    else:
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
