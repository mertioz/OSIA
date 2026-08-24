"""Bounded, transactional mobile attachment reception.

The relay, LAN and WebRTC layers all carry these messages as ordinary
authenticated application frames.  Keeping each payload small avoids creating
another transport-specific upload protocol and keeps the relay blind to file
metadata and bytes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ATTACHMENT_PROTOCOL_VERSION = 1
MAX_ATTACHMENT_FILES = 10
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ATTACHMENT_CHUNK_BYTES = 20 * 1024
MAX_CHUNKS_PER_FILE = math.ceil(MAX_ATTACHMENT_BYTES / ATTACHMENT_CHUNK_BYTES)
MAX_DISPLAY_NAME_CHARS = 180
MAX_MIME_CHARS = 127
MAX_EXTENSION_CHARS = 16
MAX_ACTIVE_BATCHES = 1
MAX_EVENT_RECORDS = MAX_ATTACHMENT_FILES * (MAX_CHUNKS_PER_FILE + 3) + 1
MAX_TOMBSTONES = 256
DEFAULT_BATCH_TTL_SECONDS = 15 * 60
ORPHAN_MAX_AGE_SECONDS = 60 * 60

ATTACHMENT_EVENT_TYPES = frozenset({
    "attachment_batch_prepare",
    "attachment_file_begin",
    "attachment_file_chunk",
    "attachment_file_complete",
    "attachment_batch_commit",
    "attachment_batch_cancel",
})

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{4,128}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}
_FORMAT_MIME = {
    "png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp",
    "gif": "image/gif", "tiff": "image/tiff", "bmp": "image/bmp",
    "wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "ogg": "audio/ogg", "flac": "audio/flac", "mp4": "video/mp4",
    "mov": "video/quicktime", "webm": "video/webm", "mkv": "video/x-matroska",
}


class AttachmentProtocolError(ValueError):
    """One stable, non-sensitive attachment failure category."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require_id(value, code: str) -> str:
    text = str(value or "")
    if not _ID_RE.fullmatch(text):
        raise AttachmentProtocolError(code)
    return text


def _require_int(value, *, minimum: int, maximum: int, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AttachmentProtocolError(code)
    if value < minimum or value > maximum:
        raise AttachmentProtocolError(code)
    return value


def _parse_timestamp(value, code: str) -> float:
    if not isinstance(value, str) or len(value) > 64:
        raise AttachmentProtocolError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttachmentProtocolError(code) from exc
    if parsed.tzinfo is None:
        raise AttachmentProtocolError(code)
    return parsed.astimezone(timezone.utc).timestamp()


def sanitize_display_name(value: str) -> str:
    """Return a bounded display-only filename, never a filesystem path."""

    name = unicodedata.normalize("NFC", str(value or ""))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if len(name) >= 2 and name[1] == ":":
        name = name[2:]
    name = "".join(
        ch for ch in name
        if unicodedata.category(ch) not in {"Cc", "Cf"}
        and ch not in '<>:"/\\|?*'
    )
    while ".." in name:
        name = name.replace("..", ".")
    name = name.strip(" .")
    if not name:
        name = "attachment.bin"
    suffix = Path(name).suffix[: MAX_EXTENSION_CHARS + 1]
    stem_limit = max(1, MAX_DISPLAY_NAME_CHARS - len(suffix))
    stem = Path(name).stem[:stem_limit].strip(" .") or "attachment"
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}{suffix}"[:MAX_DISPLAY_NAME_CHARS]


def safe_extension(display_name: str) -> str:
    suffix = Path(display_name).suffix.lower()
    if not suffix or len(suffix) > MAX_EXTENSION_CHARS + 1:
        return ""
    candidate = suffix[1:]
    return candidate if candidate.isalnum() else ""


def detect_type(path: Path, display_name: str) -> tuple[str, str]:
    """Project a byte-proved safe family/MIME; failures remain inert."""

    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_ATTACHMENT_BYTES + 1)
    except OSError as exc:
        raise AttachmentProtocolError("file_read_failed") from exc
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentProtocolError("file_too_large")
    try:
        from osia.document_identity import identify_document

        identity = identify_document(data, display_name=display_name)
        mime = _FORMAT_MIME.get(identity.format_label, "application/octet-stream")
        category = identity.family if identity.family in {"image", "audio", "video"} else "file"
        return category, mime
    except Exception:
        pass
    suffix = Path(display_name).suffix.lower()
    return "file", "application/octet-stream" if suffix not in _IMAGE_MIME else "application/octet-stream"


def detect_image_mime(data: bytes) -> str | None:
    """Return the supported image MIME proved by magic bytes, if any."""

    prefix = data[:16]
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def _canonical_fingerprint(frame: dict) -> str:
    try:
        encoded = json.dumps(
            frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AttachmentProtocolError("invalid_event") from exc
    return hashlib.sha256(encoded).hexdigest()


def _safe_remove_batch_dir(root: Path, directory: Path) -> None:
    try:
        root_resolved = root.resolve(strict=False)
        directory_resolved = directory.resolve(strict=False)
        if directory_resolved.parent != root_resolved:
            return
        if not directory.name.startswith("batch-"):
            return
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        return


def _is_safe_batch_dir(root: Path, directory: Path) -> bool:
    try:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
            return False
        is_junction = getattr(os.path, "isjunction", None)
        if callable(is_junction) and is_junction(directory):
            return False
        root_resolved = root.resolve(strict=True)
        directory_resolved = directory.resolve(strict=True)
        return (
            directory.name.startswith("batch-")
            and directory_resolved.parent == root_resolved
        )
    except OSError:
        return False


def _is_safe_storage_root(root: Path) -> bool:
    try:
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or root.is_symlink():
            return False
        is_junction = getattr(os.path, "isjunction", None)
        return not (callable(is_junction) and is_junction(root))
    except OSError:
        return False


def cleanup_orphan_batches(
    root: Path,
    *,
    now: float | None = None,
    max_entries: int = 128,
    max_age_seconds: int = ORPHAN_MAX_AGE_SECONDS,
) -> int:
    """Bounded startup cleanup that can only remove OSIA-owned batch dirs."""

    current = time.time() if now is None else now
    removed = 0
    try:
        entries = list(root.iterdir())[:max_entries]
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not entry.name.startswith("batch-") or not stat.S_ISDIR(info.st_mode):
            continue
        if entry.is_symlink() or current - info.st_mtime < max_age_seconds:
            continue
        _safe_remove_batch_dir(root, entry)
        removed += 1
    return removed


@dataclass(frozen=True)
class CommittedAttachment:
    attachment_id: str
    file_id: str
    logical_order: int
    display_name: str
    path: Path
    byte_size: int
    sha256: str
    detected_category: str
    detected_mime: str

    def persistence_metadata(self) -> dict:
        return {
            "attachment_id": self.attachment_id,
            "file_id": self.file_id,
            "logical_order": self.logical_order,
            "display_name": self.display_name,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "category": self.detected_category,
            "mime": self.detected_mime,
        }


@dataclass
class _FileState:
    file_id: str
    internal_id: str
    logical_order: int
    display_name: str
    declared_extension: str
    declared_mime: str
    declared_category: str
    declared_size: int
    declared_sha256: str
    chunk_size: int
    expected_chunk_count: int
    part_path: Path
    ready_path: Path
    handle: object | None = None
    digest: object = field(default_factory=hashlib.sha256)
    chunk_digests: dict[int, str] = field(default_factory=dict)
    next_chunk: int = 0
    bytes_written: int = 0
    begun: bool = False
    complete: bool = False
    detected_category: str = "file"
    detected_mime: str = "application/octet-stream"


@dataclass
class _BatchState:
    batch_id: str
    conversation_id: str
    request_id: str
    attachment_purpose: str
    file_count: int
    aggregate_size: int
    created_at: float
    expires_at: float
    directory: Path
    files: dict[str, _FileState]
    prepare_fingerprint: str
    accept_frame: dict
    last_sequence: int
    committed: bool = False
    consumed: bool = False
    consumed_request_id: str = ""
    commit_ack: dict | None = None


class AttachmentBatchManager:
    """Per-connection transaction manager with bounded disk and metadata state."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.root = root or Path(tempfile.gettempdir()) / "osia_attachment_batches"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _is_safe_storage_root(self.root):
            raise AttachmentProtocolError("temporary_storage_failed")
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self._clock = clock
        self._batches: dict[str, _BatchState] = {}
        self._events: OrderedDict[str, tuple[str, str, list[dict]]] = OrderedDict()
        self._tombstones: OrderedDict[str, float] = OrderedDict()

    def handle(self, frame: dict) -> list[dict]:
        event_type = str(frame.get("type") or "")
        if event_type not in ATTACHMENT_EVENT_TYPES:
            raise AttachmentProtocolError("unsupported_event")
        batch_id = str(frame.get("batch_id") or "")
        event_id = str(frame.get("event_id") or "")
        fingerprint = ""
        try:
            batch_id = _require_id(batch_id, "invalid_batch_id")
            event_id = _require_id(event_id, "invalid_event_id")
            fingerprint = _canonical_fingerprint(frame)
            previous = self._events.get(event_id)
            if previous is not None:
                previous_batch, previous_fingerprint, response = previous
                if previous_batch != batch_id or previous_fingerprint != fingerprint:
                    raise AttachmentProtocolError("event_id_conflict")
                return [dict(item) for item in response]

            if event_type == "attachment_batch_prepare":
                response = self._prepare(frame, fingerprint)
            else:
                batch = self._batches.get(batch_id)
                if batch is None:
                    raise AttachmentProtocolError("unknown_or_stale_batch")
                self._validate_sequence(batch, frame)
                response = self._dispatch(batch, frame)
            self._remember_event(event_id, batch_id, fingerprint, response)
            return response
        except AttachmentProtocolError as exc:
            if event_type != "attachment_batch_prepare":
                self._fail_batch(batch_id)
                response_type = "attachment_batch_failed"
            else:
                response_type = "attachment_batch_reject"
            response = [{
                "type": response_type,
                "protocol_version": ATTACHMENT_PROTOCOL_VERSION,
                "batch_id": batch_id if _ID_RE.fullmatch(batch_id) else "invalid",
                "client_request_id": str(frame.get("client_request_id") or ""),
                "request_id": str(frame.get("request_id") or frame.get("client_request_id") or ""),
                "event_id": f"event_{secrets.token_hex(16)}",
                "sequence": 0,
                "category": exc.code,
            }]
            if (
                fingerprint
                and _ID_RE.fullmatch(batch_id)
                and _ID_RE.fullmatch(event_id)
                and event_id not in self._events
            ):
                self._remember_event(
                    event_id, batch_id, fingerprint, response,
                )
            return response

    def _remember_event(
        self,
        event_id: str,
        batch_id: str,
        fingerprint: str,
        response: list[dict],
    ) -> None:
        self._events[event_id] = (
            batch_id, fingerprint, [dict(item) for item in response],
        )
        self._events.move_to_end(event_id)
        while len(self._events) > MAX_EVENT_RECORDS:
            self._events.popitem(last=False)

    def _prepare(self, frame: dict, fingerprint: str) -> list[dict]:
        batch_id = _require_id(frame.get("batch_id"), "invalid_batch_id")
        existing = self._batches.get(batch_id)
        if existing is not None:
            if existing.prepare_fingerprint != fingerprint:
                raise AttachmentProtocolError("batch_id_conflict")
            return [dict(existing.accept_frame)]
        if batch_id in self._tombstones:
            raise AttachmentProtocolError("stale_batch")
        if len(self._batches) >= MAX_ACTIVE_BATCHES:
            raise AttachmentProtocolError("attachment_batch_in_progress")
        if frame.get("protocol_version") != ATTACHMENT_PROTOCOL_VERSION:
            raise AttachmentProtocolError("unsupported_protocol_version")
        if frame.get("source_device_role") != "mobile":
            raise AttachmentProtocolError("invalid_source_role")
        attachment_purpose = str(frame.get("attachment_purpose") or "chat")
        if attachment_purpose not in {"chat", "autonomous"}:
            raise AttachmentProtocolError("invalid_attachment_purpose")
        conversation_id = str(frame.get("conversation_id") or "")
        if attachment_purpose == "autonomous" and conversation_id:
            raise AttachmentProtocolError("invalid_conversation_id")
        if conversation_id:
            _require_id(conversation_id, "invalid_conversation_id")
        request_id = _require_id(
            frame.get("request_id") or frame.get("client_request_id"),
            "invalid_request_id",
        )
        if str(frame.get("client_request_id") or "") != request_id:
            raise AttachmentProtocolError("request_id_mismatch")
        file_count = _require_int(
            frame.get("file_count"), minimum=1, maximum=MAX_ATTACHMENT_FILES,
            code="invalid_file_count",
        )
        aggregate_size = _require_int(
            frame.get("aggregate_size"), minimum=1,
            maximum=MAX_ATTACHMENT_BYTES, code="aggregate_too_large",
        )
        created_at = _parse_timestamp(frame.get("created_at"), "invalid_created_at")
        expires_at = _parse_timestamp(frame.get("expiry"), "invalid_expiry")
        now = self._clock()
        if expires_at <= now or expires_at <= created_at:
            raise AttachmentProtocolError("expired_batch")
        if created_at > now + 60 or expires_at - created_at > DEFAULT_BATCH_TTL_SECONDS:
            raise AttachmentProtocolError("invalid_expiry")
        sequence = _require_int(
            frame.get("sequence"), minimum=1, maximum=1, code="invalid_sequence",
        )
        raw_files = frame.get("files")
        if not isinstance(raw_files, list) or len(raw_files) != file_count:
            raise AttachmentProtocolError("file_count_mismatch")

        directory = Path(tempfile.mkdtemp(prefix="batch-", dir=self.root))
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        files: dict[str, _FileState] = {}
        orders: set[int] = set()
        declared_total = 0
        try:
            for raw in raw_files:
                if not isinstance(raw, dict):
                    raise AttachmentProtocolError("invalid_file_metadata")
                file_id = _require_id(raw.get("file_id"), "invalid_file_id")
                if file_id in files:
                    raise AttachmentProtocolError("duplicate_file_id")
                order = _require_int(
                    raw.get("logical_order"), minimum=0,
                    maximum=file_count - 1, code="invalid_logical_order",
                )
                if order in orders:
                    raise AttachmentProtocolError("duplicate_logical_order")
                orders.add(order)
                display_name = sanitize_display_name(str(raw.get("display_name") or ""))
                declared_extension = str(raw.get("original_extension") or "").lower()
                if len(declared_extension) > MAX_EXTENSION_CHARS or (
                    declared_extension and not declared_extension.isalnum()
                ):
                    raise AttachmentProtocolError("invalid_extension")
                if declared_extension and declared_extension != safe_extension(display_name):
                    raise AttachmentProtocolError("extension_mismatch")
                declared_mime = str(raw.get("declared_mime") or "application/octet-stream")
                if not declared_mime or len(declared_mime) > MAX_MIME_CHARS:
                    raise AttachmentProtocolError("invalid_mime")
                declared_category = str(raw.get("detected_type_category") or "file")
                if declared_category not in {
                    "file", "image", "audio", "video", "text", "document",
                }:
                    raise AttachmentProtocolError("invalid_type_category")
                declared_size = _require_int(
                    raw.get("declared_byte_size"), minimum=1,
                    maximum=MAX_ATTACHMENT_BYTES, code="file_too_large",
                )
                declared_total += declared_size
                if declared_total > MAX_ATTACHMENT_BYTES:
                    raise AttachmentProtocolError("aggregate_too_large")
                declared_hash = str(raw.get("sha256") or "").lower()
                if not _HASH_RE.fullmatch(declared_hash):
                    raise AttachmentProtocolError("invalid_hash")
                chunk_size = _require_int(
                    raw.get("chunk_size"), minimum=1,
                    maximum=ATTACHMENT_CHUNK_BYTES, code="invalid_chunk_size",
                )
                expected_chunks = _require_int(
                    raw.get("expected_chunk_count"), minimum=1,
                    maximum=MAX_CHUNKS_PER_FILE, code="excessive_chunk_count",
                )
                if expected_chunks != math.ceil(declared_size / chunk_size):
                    raise AttachmentProtocolError("chunk_count_mismatch")
                internal_id = f"att_{secrets.token_hex(16)}"
                files[file_id] = _FileState(
                    file_id=file_id,
                    internal_id=internal_id,
                    logical_order=order,
                    display_name=display_name,
                    declared_extension=declared_extension,
                    declared_mime=declared_mime,
                    declared_category=declared_category,
                    declared_size=declared_size,
                    declared_sha256=declared_hash,
                    chunk_size=chunk_size,
                    expected_chunk_count=expected_chunks,
                    part_path=directory / f"{internal_id}.part",
                    ready_path=directory / f"{internal_id}.ready",
                )
            if orders != set(range(file_count)):
                raise AttachmentProtocolError("logical_order_gap")
            if declared_total != aggregate_size:
                raise AttachmentProtocolError("aggregate_mismatch")
        except Exception:
            _safe_remove_batch_dir(self.root, directory)
            raise

        accept = {
            "type": "attachment_batch_accept",
            "protocol_version": ATTACHMENT_PROTOCOL_VERSION,
            "batch_id": batch_id,
            "conversation_id": conversation_id,
            "client_request_id": request_id,
            "request_id": request_id,
            "event_id": f"event_{secrets.token_hex(16)}",
            "sequence": sequence,
            "file_count": file_count,
            "aggregate_size": aggregate_size,
            "attachment_purpose": attachment_purpose,
        }
        self._batches[batch_id] = _BatchState(
            batch_id=batch_id,
            conversation_id=conversation_id,
            request_id=request_id,
            attachment_purpose=attachment_purpose,
            file_count=file_count,
            aggregate_size=aggregate_size,
            created_at=created_at,
            expires_at=expires_at,
            directory=directory,
            files=files,
            prepare_fingerprint=fingerprint,
            accept_frame=accept,
            last_sequence=sequence,
        )
        return [dict(accept)]

    def _validate_sequence(self, batch: _BatchState, frame: dict) -> None:
        if self._clock() >= batch.expires_at:
            raise AttachmentProtocolError("expired_batch")
        sequence = _require_int(
            frame.get("sequence"), minimum=1, maximum=2**31 - 1,
            code="invalid_sequence",
        )
        if sequence != batch.last_sequence + 1:
            raise AttachmentProtocolError("out_of_order_event")
        if str(frame.get("client_request_id") or "") != batch.request_id:
            raise AttachmentProtocolError("request_id_mismatch")
        if str(frame.get("request_id") or "") != batch.request_id:
            raise AttachmentProtocolError("request_id_mismatch")
        conversation_id = str(frame.get("conversation_id") or "")
        if conversation_id != batch.conversation_id:
            raise AttachmentProtocolError("conversation_id_mismatch")
        batch.last_sequence = sequence

    def _dispatch(self, batch: _BatchState, frame: dict) -> list[dict]:
        event_type = str(frame.get("type") or "")
        if event_type == "attachment_file_begin":
            self._file_begin(batch, frame)
            return []
        if event_type == "attachment_file_chunk":
            self._file_chunk(batch, frame)
            return []
        if event_type == "attachment_file_complete":
            self._file_complete(batch, frame)
            return []
        if event_type == "attachment_batch_commit":
            return [self._commit(batch, frame)]
        if event_type == "attachment_batch_cancel":
            self._cancel(batch.batch_id)
            return [{
                "type": "attachment_batch_failed",
                "protocol_version": ATTACHMENT_PROTOCOL_VERSION,
                "batch_id": batch.batch_id,
                "conversation_id": batch.conversation_id,
                "client_request_id": batch.request_id,
                "request_id": batch.request_id,
                "event_id": f"event_{secrets.token_hex(16)}",
                "sequence": batch.last_sequence,
                "category": "canceled",
            }]
        raise AttachmentProtocolError("unsupported_event")

    def _get_file(self, batch: _BatchState, frame: dict) -> _FileState:
        file_id = _require_id(frame.get("file_id"), "invalid_file_id")
        file_state = batch.files.get(file_id)
        if file_state is None:
            raise AttachmentProtocolError("unknown_file")
        return file_state

    def _file_begin(self, batch: _BatchState, frame: dict) -> None:
        file_state = self._get_file(batch, frame)
        if batch.committed or file_state.complete:
            raise AttachmentProtocolError("invalid_file_state")
        logical_order = _require_int(
            frame.get("logical_order"), minimum=0,
            maximum=batch.file_count - 1, code="invalid_logical_order",
        )
        if logical_order != file_state.logical_order:
            raise AttachmentProtocolError("logical_order_mismatch")
        if file_state.begun:
            return
        if not _is_safe_batch_dir(self.root, batch.directory):
            raise AttachmentProtocolError("temporary_storage_failed")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(file_state.part_path, flags, 0o600)
            file_state.handle = os.fdopen(fd, "wb", buffering=0)
        except OSError as exc:
            raise AttachmentProtocolError("temporary_storage_failed") from exc
        file_state.begun = True

    def _file_chunk(self, batch: _BatchState, frame: dict) -> None:
        file_state = self._get_file(batch, frame)
        if not file_state.begun or file_state.complete or file_state.handle is None:
            raise AttachmentProtocolError("invalid_file_state")
        chunk_index = _require_int(
            frame.get("chunk_index"), minimum=0,
            maximum=file_state.expected_chunk_count - 1,
            code="invalid_chunk_index",
        )
        offset = _require_int(
            frame.get("byte_offset"), minimum=0,
            maximum=file_state.declared_size, code="invalid_chunk_offset",
        )
        if offset != chunk_index * file_state.chunk_size:
            raise AttachmentProtocolError("invalid_chunk_offset")
        encoded = frame.get("data")
        if not isinstance(encoded, str) or len(encoded) > (
            (ATTACHMENT_CHUNK_BYTES + 2) // 3 * 4 + 4
        ):
            raise AttachmentProtocolError("chunk_too_large")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentProtocolError("invalid_chunk_encoding") from exc
        expected_size = min(
            file_state.chunk_size,
            file_state.declared_size - offset,
        )
        payload_length = _require_int(
            frame.get("payload_length"), minimum=1,
            maximum=ATTACHMENT_CHUNK_BYTES, code="invalid_payload_length",
        )
        expected_chunks = _require_int(
            frame.get("expected_chunk_count"), minimum=1,
            maximum=MAX_CHUNKS_PER_FILE, code="excessive_chunk_count",
        )
        if expected_chunks != file_state.expected_chunk_count:
            raise AttachmentProtocolError("chunk_count_mismatch")
        if (
            len(payload) != expected_size
            or payload_length != len(payload)
            or not payload
        ):
            raise AttachmentProtocolError("invalid_chunk_length")
        payload_digest = hashlib.sha256(payload).hexdigest()
        previous = file_state.chunk_digests.get(chunk_index)
        if previous is not None:
            if previous != payload_digest:
                raise AttachmentProtocolError("conflicting_duplicate_chunk")
            return
        if chunk_index != file_state.next_chunk:
            raise AttachmentProtocolError("out_of_order_chunk")
        if file_state.bytes_written + len(payload) > file_state.declared_size:
            raise AttachmentProtocolError("bytes_beyond_declared_size")
        try:
            written = file_state.handle.write(payload)
        except OSError as exc:
            raise AttachmentProtocolError("temporary_storage_failed") from exc
        if written != len(payload):
            raise AttachmentProtocolError("temporary_storage_failed")
        file_state.digest.update(payload)
        file_state.chunk_digests[chunk_index] = payload_digest
        file_state.next_chunk += 1
        file_state.bytes_written += len(payload)

    def _file_complete(self, batch: _BatchState, frame: dict) -> None:
        file_state = self._get_file(batch, frame)
        declared_size = _require_int(
            frame.get("declared_byte_size"), minimum=1,
            maximum=MAX_ATTACHMENT_BYTES, code="file_too_large",
        )
        declared_hash = str(frame.get("sha256") or "").lower()
        if declared_size != file_state.declared_size:
            raise AttachmentProtocolError("size_mismatch")
        if declared_hash != file_state.declared_sha256:
            raise AttachmentProtocolError("hash_mismatch")
        if file_state.complete:
            return
        if not file_state.begun or file_state.handle is None:
            raise AttachmentProtocolError("invalid_file_state")
        if file_state.next_chunk != file_state.expected_chunk_count:
            raise AttachmentProtocolError("missing_chunks")
        if file_state.bytes_written != file_state.declared_size:
            raise AttachmentProtocolError("size_mismatch")
        try:
            file_state.handle.flush()
            os.fsync(file_state.handle.fileno())
            file_state.handle.close()
        except OSError as exc:
            raise AttachmentProtocolError("temporary_storage_failed") from exc
        finally:
            file_state.handle = None
        if file_state.digest.hexdigest() != file_state.declared_sha256:
            raise AttachmentProtocolError("hash_mismatch")
        if not _is_safe_batch_dir(self.root, batch.directory):
            raise AttachmentProtocolError("temporary_storage_failed")
        try:
            os.replace(file_state.part_path, file_state.ready_path)
            detected_category, detected_mime = detect_type(
                file_state.ready_path, file_state.display_name,
            )
        except OSError as exc:
            raise AttachmentProtocolError("temporary_storage_failed") from exc
        file_state.detected_category = detected_category
        file_state.detected_mime = detected_mime
        file_state.complete = True

    def _commit(self, batch: _BatchState, frame: dict) -> dict:
        file_count = _require_int(
            frame.get("file_count"), minimum=1, maximum=MAX_ATTACHMENT_FILES,
            code="invalid_file_count",
        )
        aggregate_size = _require_int(
            frame.get("aggregate_size"), minimum=1,
            maximum=MAX_ATTACHMENT_BYTES, code="aggregate_too_large",
        )
        if file_count != batch.file_count:
            raise AttachmentProtocolError("file_count_mismatch")
        if aggregate_size != batch.aggregate_size:
            raise AttachmentProtocolError("aggregate_mismatch")
        if batch.committed:
            return dict(batch.commit_ack or {})
        ordered = sorted(batch.files.values(), key=lambda item: item.logical_order)
        if not all(item.complete for item in ordered):
            raise AttachmentProtocolError("incomplete_batch")
        if sum(item.bytes_written for item in ordered) != batch.aggregate_size:
            raise AttachmentProtocolError("aggregate_mismatch")
        attachments = [
            {
                "attachment_id": item.internal_id,
                "file_id": item.file_id,
                "logical_order": item.logical_order,
                "byte_size": item.bytes_written,
                "hash_match": True,
            }
            for item in ordered
        ]
        ack = {
            "type": "attachment_batch_commit_ack",
            "protocol_version": ATTACHMENT_PROTOCOL_VERSION,
            "batch_id": batch.batch_id,
            "conversation_id": batch.conversation_id,
            "client_request_id": batch.request_id,
            "request_id": batch.request_id,
            "event_id": f"event_{secrets.token_hex(16)}",
            "sequence": batch.last_sequence,
            "file_count": batch.file_count,
            "aggregate_size": batch.aggregate_size,
            "attachments": attachments,
            "attachment_purpose": batch.attachment_purpose,
        }
        batch.committed = True
        batch.commit_ack = ack
        return dict(ack)

    def consume_for_turn(
        self,
        batch_id: str,
        conversation_id: str,
        request_id: str,
    ) -> list[CommittedAttachment]:
        batch = self._batches.get(str(batch_id or ""))
        if batch is not None and batch.attachment_purpose != "chat":
            raise AttachmentProtocolError("attachment_purpose_mismatch")
        batch = self.validate_turn_reference(
            batch_id,
            conversation_id,
            request_id,
            allow_new_conversation_binding=True,
        )
        if not batch.conversation_id:
            batch.conversation_id = conversation_id
        batch.consumed = True
        batch.consumed_request_id = request_id
        return [
            CommittedAttachment(
                attachment_id=item.internal_id,
                file_id=item.file_id,
                logical_order=item.logical_order,
                display_name=item.display_name,
                path=item.ready_path,
                byte_size=item.bytes_written,
                sha256=item.declared_sha256,
                detected_category=item.detected_category,
                detected_mime=item.detected_mime,
            )
            for item in sorted(batch.files.values(), key=lambda value: value.logical_order)
        ]

    def consume_for_autonomous(
        self,
        batch_id: str,
        request_id: str,
    ) -> list[CommittedAttachment]:
        """Consume one committed Autonomous batch without Chat binding."""

        batch = self.validate_turn_reference(batch_id, "", request_id)
        if batch.attachment_purpose != "autonomous":
            raise AttachmentProtocolError("attachment_purpose_mismatch")
        batch.consumed = True
        batch.consumed_request_id = request_id
        return [
            CommittedAttachment(
                attachment_id=item.internal_id,
                file_id=item.file_id,
                logical_order=item.logical_order,
                display_name=item.display_name,
                path=item.ready_path,
                byte_size=item.bytes_written,
                sha256=item.declared_sha256,
                detected_category=item.detected_category,
                detected_mime=item.detected_mime,
            )
            for item in sorted(
                batch.files.values(), key=lambda value: value.logical_order,
            )
        ]

    def validate_turn_reference(
        self,
        batch_id: str,
        conversation_id: str,
        request_id: str,
        *,
        allow_new_conversation_binding: bool = False,
    ) -> _BatchState:
        """Validate a committed batch without consuming it.

        A new chat prepares its batch before the server has allocated a
        conversation id.  The receive loop preflights that empty id before it
        creates a conversation, then ``consume_for_turn`` binds the resulting
        id once.  Existing conversations must match exactly.
        """

        batch_id = _require_id(batch_id, "invalid_batch_id")
        batch = self._batches.get(batch_id)
        if batch is None or not batch.committed:
            raise AttachmentProtocolError("attachment_batch_not_committed")
        if batch.conversation_id != conversation_id and not (
            allow_new_conversation_binding
            and not batch.conversation_id
            and bool(conversation_id)
        ):
            raise AttachmentProtocolError("conversation_id_mismatch")
        if batch.request_id != request_id:
            raise AttachmentProtocolError("request_id_mismatch")
        if batch.consumed:
            raise AttachmentProtocolError("attachment_batch_already_consumed")
        return batch

    def release(self, batch_id: str) -> bool:
        batch = self._batches.pop(batch_id, None)
        if batch is None:
            return False
        self._close_handles(batch)
        _safe_remove_batch_dir(self.root, batch.directory)
        self._remember_tombstone(batch_id)
        return True

    def cleanup_disconnected(self) -> int:
        """Remove partial/unconsumed batches; active turns own consumed ones."""

        removed = 0
        for batch_id, batch in list(self._batches.items()):
            if batch.consumed:
                continue
            if self.release(batch_id):
                removed += 1
        return removed

    def expire(self) -> int:
        now = self._clock()
        removed = 0
        for batch_id, batch in list(self._batches.items()):
            if batch.consumed or now < batch.expires_at:
                continue
            if self.release(batch_id):
                removed += 1
        for batch_id, expiry in list(self._tombstones.items()):
            if expiry <= now:
                self._tombstones.pop(batch_id, None)
        return removed

    def _cancel(self, batch_id: str) -> None:
        self.release(batch_id)

    def _fail_batch(self, batch_id: str) -> None:
        if _ID_RE.fullmatch(batch_id):
            self.release(batch_id)

    def _close_handles(self, batch: _BatchState) -> None:
        for item in batch.files.values():
            if item.handle is None:
                continue
            try:
                item.handle.close()
            except OSError:
                pass
            item.handle = None

    def _remember_tombstone(self, batch_id: str) -> None:
        self._tombstones[batch_id] = self._clock() + DEFAULT_BATCH_TTL_SECONDS
        self._tombstones.move_to_end(batch_id)
        while len(self._tombstones) > MAX_TOMBSTONES:
            self._tombstones.popitem(last=False)


def _build_image_content(prompt: str, images: list[tuple[str, str, bytes]]) -> list[dict]:
    image_names = ", ".join(name for name, _mime, _data in images)
    text = (
        "[Images jointes présentes dans les parties visuelles qui suivent : "
        f"{image_names}. Si le fournisseur sélectionné ne donne pas accès "
        "aux parties visuelles, signale explicitement cette incapacité du "
        "fournisseur ; ne prétends pas qu'aucune image n'a été jointe.]\n\n"
        f"{prompt.strip()}"
    ).strip()
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for _name, mime, data in images:
        encoded = base64.b64encode(data).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        })
    return content


def build_local_attachment_input(
    prompt: str,
    path: str | Path,
    *,
    owner_id: str | None = None,
) -> tuple[str | list[dict], str]:
    """Capture one desktop attachment before its selected path can disappear."""

    source = Path(path)
    display_name = sanitize_display_name(source.name)
    try:
        before = source.stat()
        if not source.is_file() or before.st_size <= 0:
            raise AttachmentProtocolError("file_read_failed")
        if before.st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentProtocolError("file_too_large")
        data = source.read_bytes()
    except AttachmentProtocolError:
        raise
    except OSError as exc:
        raise AttachmentProtocolError("file_read_failed") from exc
    if len(data) != before.st_size:
        raise AttachmentProtocolError("file_size_mismatch")

    from osia.document_sources import get_source_store

    if owner_id:
        capability = get_source_store().create(
            source,
            owner_id=owner_id,
            source_kind="desktop_attachment",
            display_name=display_name,
        )
    else:
        capability = get_source_store().create_for_current_owner(
            source,
            source_kind="desktop_attachment",
            display_name=display_name,
        )
    text = (
        "[Document attachment available through structured tools: "
        f"source_ref={capability['source_ref']} "
        f"type={capability['type_label']} state={capability['state']}]"
    )
    if prompt.strip():
        text = f"{text}\n\n{prompt.strip()}"
    return text, display_name


def build_agent_input(
    prompt: str,
    attachments: list[CommittedAttachment],
    *,
    owner_id: str | None = None,
    permission_epoch: int | None = None,
    source_refs_out: list[str] | None = None,
) -> str | list[dict]:
    """Create opaque local sources before any provider input."""

    ordered = sorted(attachments, key=lambda item: item.logical_order)
    sources = list(ordered)
    text = prompt.strip()
    if sources:
        from osia.document_sources import get_source_store

        capabilities = []
        for item in sources:
            if owner_id:
                capability = get_source_store().create(
                    item.path,
                    owner_id=owner_id,
                    source_kind="mobile_attachment",
                    display_name=item.display_name,
                    declared_mime=item.detected_mime,
                    permission_epoch=permission_epoch,
                )
            else:
                capability = get_source_store().create_for_current_owner(
                    item.path,
                    source_kind="mobile_attachment",
                    display_name=item.display_name,
                    declared_mime=item.detected_mime,
                    permission_epoch=permission_epoch,
                )
            capabilities.append(capability)
            if source_refs_out is not None:
                source_refs_out.append(str(capability["source_ref"]))
        bullets = "\n".join(
            f"{index}. source_ref={item['source_ref']} type={item['type_label']} "
            f"state={item['state']}"
            + (f" category={item['category']}" if item.get("category") else "")
            for index, item in enumerate(capabilities, start=1)
        )
        text = (
            "[Document/media attachments in original order. Use read_document with "
            "each opaque source_ref, then query_document; never request or "
            "infer a local path:\n"
            f"{bullets}\n]\n\n{text}"
        ).strip()
    return text
