"""Bounded, ownership-aware storage for legacy ``type: \"file\"`` frames.

The transactional attachment protocol in :mod:`osia.attachments` remains the
preferred path.  This module only contains the compatibility boundary needed
by older authenticated clients that still send one Base64 payload in a JSON
frame.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Awaitable, Callable, TypeVar

from osia.attachments import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENT_FILES


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_OWNED_NAME_RE = re.compile(r"legacy-[0-9a-f]{32}(?:\.[a-z0-9]{1,16})?")
_OLD_OWNED_NAME_RE = re.compile(r"[0-9]{13}_.+")
_MAX_DISPLAY_NAME_CHARS = 180
_MAX_STORAGE_ATTEMPTS = 128
_T = TypeVar("_T")


class LegacyUploadError(ValueError):
    """A sanitized legacy-upload rejection suitable for a protocol frame."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LegacyUpload:
    """One server-owned legacy upload."""

    path: Path
    display_name: str
    byte_size: int


def safe_legacy_display_name(filename: str) -> str:
    """Return a portable display-only basename for an untrusted filename."""

    # Treat both separator styles as separators on every host.  ``basename``
    # alone would leave Windows backslashes intact when the PC runs on Linux.
    name = re.split(r"[\\/]", filename)[-1]
    name = "".join(
        char
        for char in name
        if char not in '<>:"/\\|?*' and ord(char) >= 32
    ).strip(" .")
    if not name or name in {".", ".."}:
        return "upload.bin"

    stem = name.split(".", 1)[0].rstrip(" .")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"

    if len(name) > _MAX_DISPLAY_NAME_CHARS:
        extension = _safe_extension(name)
        stem_limit = _MAX_DISPLAY_NAME_CHARS - len(extension)
        name = f"{name[:stem_limit].rstrip(' .')}{extension}"
    return name or "upload.bin"


def _safe_extension(display_name: str) -> str:
    """Return a small sanitized extension for a server-generated filename."""

    if "." not in display_name:
        return ""
    extension = display_name.rsplit(".", 1)[1].lower()
    if not extension or len(extension) > 16:
        return ""
    if not all(char.isascii() and char.isalnum() for char in extension):
        return ""
    return f".{extension}"


def _decoded_size_preflight(encoded_data: str, maximum: int) -> tuple[bytes, int]:
    """Validate Base64 shape and calculate decoded bytes without decoding."""

    maximum_encoded = 4 * math.ceil(maximum / 3)
    if len(encoded_data) > maximum_encoded:
        raise LegacyUploadError("encoded_payload_too_large")
    if not encoded_data or len(encoded_data) % 4:
        raise LegacyUploadError("invalid_base64")
    try:
        encoded_bytes = encoded_data.encode("ascii")
    except UnicodeEncodeError as exc:
        raise LegacyUploadError("invalid_base64") from exc

    padding = len(encoded_data) - len(encoded_data.rstrip("="))
    if padding > 2 or "=" in encoded_data[:-padding or None]:
        raise LegacyUploadError("invalid_base64")
    decoded_size = (len(encoded_data) // 4) * 3 - padding
    if decoded_size > maximum:
        raise LegacyUploadError("decoded_payload_too_large")
    return encoded_bytes, decoded_size


def _declared_size(frame: dict, estimated_size: int) -> None:
    values = [
        frame[key]
        for key in ("declared_byte_size", "size")
        if key in frame
    ]
    if not values:
        return
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ):
        raise LegacyUploadError("invalid_declared_size")
    if any(value != values[0] for value in values):
        raise LegacyUploadError("declared_size_mismatch")
    if values[0] != estimated_size:
        raise LegacyUploadError("declared_size_mismatch")


class LegacyUploadManager:
    """Own legacy files for one authenticated WebSocket connection."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = MAX_ATTACHMENT_FILES,
        max_bytes: int = MAX_ATTACHMENT_BYTES,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_files = max_files
        self.max_bytes = max_bytes
        self._token_factory = token_factory
        self._lock = threading.Lock()
        self._owned: dict[Path, int] = {}
        self._reserved_files = 0
        self._reserved_bytes = 0

    @property
    def outstanding_count(self) -> int:
        with self._lock:
            return len(self._owned) + self._reserved_files

    def save_frame(self, frame: dict) -> LegacyUpload:
        """Validate and persist one legacy upload using exclusive creation."""

        if not isinstance(frame, dict):
            raise LegacyUploadError("invalid_payload")
        filename = frame.get("filename", "upload.bin")
        encoded_data = frame.get("data")
        if not isinstance(filename, str):
            raise LegacyUploadError("invalid_filename")
        if not isinstance(encoded_data, str):
            raise LegacyUploadError("invalid_payload")

        display_name = safe_legacy_display_name(filename)
        encoded_bytes, estimated_size = _decoded_size_preflight(
            encoded_data,
            self.max_bytes,
        )
        _declared_size(frame, estimated_size)

        with self._lock:
            file_count = len(self._owned) + self._reserved_files
            if file_count >= self.max_files:
                raise LegacyUploadError("too_many_files")
            aggregate = (
                sum(self._owned.values())
                + self._reserved_bytes
                + estimated_size
            )
            if aggregate > self.max_bytes:
                raise LegacyUploadError("aggregate_too_large")
            self._reserved_files += 1
            self._reserved_bytes += estimated_size

        target: Path | None = None
        try:
            try:
                decoded = base64.b64decode(encoded_bytes, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise LegacyUploadError("invalid_base64") from exc
            if len(decoded) != estimated_size:
                raise LegacyUploadError("decoded_size_mismatch")

            self.root.mkdir(parents=True, exist_ok=True)
            extension = _safe_extension(display_name)
            for _attempt in range(_MAX_STORAGE_ATTEMPTS):
                token = self._token_factory(16)
                if not isinstance(token, str) or not re.fullmatch(
                    r"[0-9a-f]{32}", token
                ):
                    raise LegacyUploadError("storage_name_generation_failed")
                candidate = self.root / f"legacy-{token}{extension}"
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    if hasattr(os, "O_BINARY"):
                        flags |= os.O_BINARY
                    descriptor = os.open(candidate, flags, 0o600)
                except FileExistsError:
                    continue
                target = candidate
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(decoded)
                        output.flush()
                        os.fsync(output.fileno())
                except BaseException:
                    try:
                        candidate.unlink()
                    except OSError:
                        pass
                    raise
                break
            if target is None:
                raise LegacyUploadError("storage_collision")

            with self._lock:
                self._owned[target] = estimated_size
            return LegacyUpload(target, display_name, estimated_size)
        finally:
            with self._lock:
                self._reserved_files -= 1
                self._reserved_bytes -= estimated_size

    def cleanup(self, paths: list[str | Path] | tuple[str | Path, ...]) -> int:
        """Idempotently remove only files owned by this connection."""

        removed = 0
        for value in dict.fromkeys(Path(path) for path in paths):
            try:
                candidate = value.resolve()
            except OSError:
                continue
            if candidate.parent != self.root:
                continue
            with self._lock:
                if candidate not in self._owned:
                    continue
                self._owned.pop(candidate, None)
            try:
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                # Ownership is dropped so this connection cannot repeatedly
                # target the path.  Startup reconciliation handles a leftover
                # file bearing the server-owned name on the next launch.
                pass
        return removed


async def run_with_legacy_cleanup(
    manager: LegacyUploadManager,
    paths: list[str | Path] | tuple[str | Path, ...],
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """Run a turn operation and release its legacy files on every exit path."""

    try:
        return await operation()
    finally:
        manager.cleanup(paths)


def reconcile_legacy_uploads(root: str | Path) -> int:
    """Remove restart-orphaned legacy files without leaving ``root``.

    Only regular, non-symlink files carrying the current cryptographic prefix
    or the exact 13-digit prefix written by the previous legacy implementation
    are eligible.  Directories, transactional batches, unrelated files and
    anything outside this one dedicated directory are never traversed.
    """

    directory = Path(root)
    try:
        entries = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return 0

    removed = 0
    for entry in entries:
        if not (
            _OWNED_NAME_RE.fullmatch(entry.name)
            or _OLD_OWNED_NAME_RE.fullmatch(entry.name)
        ):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed
