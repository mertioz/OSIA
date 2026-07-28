"""Private opaque browser upload/download artifacts for WOR-186."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from osia.config import PRIVATE_DATA_DIR as _PRIVATE_DATA_DIR


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_SESSION_TEMP_BYTES = 4 * 1024 * 1024 * 1024
ARTIFACT_TTL_SECONDS = 72 * 60 * 60
BROWSER_ARTIFACT_ROOT = Path(_PRIVATE_DATA_DIR) / "browser_artifacts"

_ARTIFACT_RE = re.compile(r"^web_[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_EXTENSIONS = frozenset({
    ".apk", ".app", ".bat", ".cmd", ".com", ".dll", ".dmg", ".exe",
    ".hta", ".jar", ".js", ".jse", ".lnk", ".msi", ".msp", ".ps1",
    ".py", ".reg", ".scr", ".sh", ".vbe", ".vbs", ".wsf",
})
_ARCHIVE_EXTENSIONS = frozenset({
    ".7z", ".bz2", ".cab", ".gz", ".iso", ".rar", ".tar", ".tgz",
    ".xz", ".zip",
})
_MACRO_EXTENSIONS = frozenset({".docm", ".dotm", ".pptm", ".potm", ".xlsm", ".xltm"})


class BrowserArtifactError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_reparse(path: Path) -> bool:
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & marker)


def _reject_indirect_path(path: Path) -> None:
    current = path
    while True:
        try:
            if current.is_symlink() or _is_reparse(current):
                raise BrowserArtifactError("indirect_path_rejected")
        except OSError as exc:
            raise BrowserArtifactError("file_unavailable") from exc
        parent = current.parent
        if parent == current:
            break
        current = parent


def _sha256_file(path: Path, *, limit: int) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    total = 0
    prefix = b""
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                if not prefix:
                    prefix = block[:512]
                total += len(block)
                if total > limit:
                    raise BrowserArtifactError("file_size_limit_exceeded")
                digest.update(block)
    except BrowserArtifactError:
        raise
    except OSError as exc:
        raise BrowserArtifactError("file_unavailable") from exc
    return total, digest.hexdigest(), prefix


def detect_content(prefix: bytes, filename: str = "") -> tuple[str, str]:
    lower = str(filename or "").lower()
    extension = Path(lower).suffix
    mime = "application/octet-stream"
    if prefix.startswith(b"%PDF-"):
        mime = "application/pdf"
    elif prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif prefix.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif prefix.startswith(b"GIF87a") or prefix.startswith(b"GIF89a"):
        mime = "image/gif"
    elif prefix.startswith(b"PK\x03\x04"):
        mime = "application/zip"
    elif prefix.startswith(b"MZ"):
        mime = "application/vnd.microsoft.portable-executable"
    elif prefix.startswith(b"\x7fELF"):
        mime = "application/x-executable"
    elif prefix.startswith(b"#!"):
        mime = "text/x-script"
    else:
        guessed, _encoding = mimetypes.guess_type(lower)
        if guessed:
            mime = guessed[:160]
    if (
        extension in _ACTIVE_EXTENSIONS
        or extension in _MACRO_EXTENSIONS
        or mime in {
            "application/vnd.microsoft.portable-executable",
            "application/x-executable", "text/x-script",
        }
    ):
        classification = "active_content"
    elif extension in _ARCHIVE_EXTENSIONS or mime in {
        "application/zip", "application/x-7z-compressed", "application/x-rar-compressed",
    }:
        classification = "archive_content"
    else:
        classification = "opaque_content"
    return mime, classification


@dataclass(frozen=True)
class ValidatedUpload:
    canonical_path: Path
    size: int
    sha256: str
    mime_type: str
    classification: str
    device: int
    inode: int
    modified_ns: int
    changed_ns: int

    def safe_metadata(self) -> dict:
        return {
            "size": self.size,
            "mime_type": self.mime_type,
            "classification": self.classification,
        }


def validate_upload_path(value: str | os.PathLike[str]) -> ValidatedUpload:
    raw = Path(value)
    if not raw.is_absolute():
        raise BrowserArtifactError("absolute_path_required")
    _reject_indirect_path(raw)
    try:
        canonical = raw.resolve(strict=True)
        info = canonical.stat()
    except OSError as exc:
        raise BrowserArtifactError("file_unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BrowserArtifactError("regular_file_required")
    if info.st_size < 0 or info.st_size > MAX_UPLOAD_BYTES:
        raise BrowserArtifactError("file_size_limit_exceeded")
    size, digest, prefix = _sha256_file(canonical, limit=MAX_UPLOAD_BYTES)
    if size != info.st_size:
        raise BrowserArtifactError("file_changed")
    mime, classification = detect_content(prefix, canonical.name)
    return ValidatedUpload(
        canonical_path=canonical,
        size=size,
        sha256=digest,
        mime_type=mime,
        classification=classification,
        device=int(info.st_dev),
        inode=int(info.st_ino),
        modified_ns=int(info.st_mtime_ns),
        changed_ns=int(info.st_ctime_ns),
    )


def revalidate_upload(value: ValidatedUpload) -> ValidatedUpload:
    fresh = validate_upload_path(value.canonical_path)
    if fresh != value:
        raise BrowserArtifactError("file_changed")
    return fresh


def sanitize_suggested_filename(value: str) -> str:
    name = Path(str(value or "download").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).strip(" .")
    if not name:
        name = "download"
    stem, suffix = os.path.splitext(name)
    stem = stem[:96] or "download"
    suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:16]
    return stem + suffix


class BrowserArtifactStore:
    def __init__(self, root: Path = BROWSER_ARTIFACT_ROOT, *, clock=None):
        self.root = Path(root)
        self._clock = clock or time.time
        self._lock = threading.RLock()

    @staticmethod
    def _chmod_private(path: Path, *, directory=False) -> None:
        try:
            os.chmod(path, stat.S_IRWXU if directory else stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._chmod_private(self.root, directory=True)

    def finalize_download(self, source: Path, *, suggested_filename: str) -> dict:
        source = Path(source)
        if source.is_symlink() or _is_reparse(source):
            raise BrowserArtifactError("indirect_path_rejected")
        try:
            info = source.stat()
        except OSError as exc:
            raise BrowserArtifactError("download_missing") from exc
        if not stat.S_ISREG(info.st_mode):
            raise BrowserArtifactError("regular_file_required")
        if info.st_size < 0 or info.st_size > MAX_DOWNLOAD_BYTES:
            raise BrowserArtifactError("file_size_limit_exceeded")
        size, digest, prefix = _sha256_file(source, limit=MAX_DOWNLOAD_BYTES)
        safe_name = sanitize_suggested_filename(suggested_filename)
        mime, classification = detect_content(prefix, safe_name)
        artifact_id = "web_" + os.urandom(16).hex()
        created_at = int(self._clock())
        reference = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "size": size,
            "sha256": digest,
            "mime_type": mime,
            "classification": classification,
            "created_at": created_at,
            "expires_at": created_at + ARTIFACT_TTL_SECONDS,
            "provenance": "browser_download",
            "private": True,
            "executable": False,
            "extractable": False,
        }
        stored = {**reference, "private_filename": safe_name}
        encoded = json.dumps(
            stored, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > 8_192:
            raise BrowserArtifactError("artifact_metadata_limit")
        with self._lock:
            self._ensure_root()
            temporary = self.root / f".{artifact_id}.{os.urandom(8).hex()}.tmp"
            final = self.root / artifact_id
            try:
                temporary.mkdir()
                self._chmod_private(temporary, directory=True)
                payload = temporary / "payload.bin"
                self._copy_bounded(source, payload, MAX_DOWNLOAD_BYTES)
                metadata_path = temporary / "metadata.json"
                with metadata_path.open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._chmod_private(payload)
                self._chmod_private(metadata_path)
                os.replace(temporary, final)
                self._chmod_private(final, directory=True)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return reference

    @staticmethod
    def _copy_bounded(source: Path, target: Path, limit: int) -> None:
        total = 0
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > limit:
                        raise BrowserArtifactError("file_size_limit_exceeded")
                    dst.write(block)
                dst.flush()
                os.fsync(dst.fileno())
        except BrowserArtifactError:
            raise
        except OSError as exc:
            raise BrowserArtifactError("artifact_write_failed") from exc

    def resolve_private_path(self, artifact_id: str) -> Path:
        if not _ARTIFACT_RE.fullmatch(str(artifact_id or "")):
            raise BrowserArtifactError("invalid_artifact_id")
        directory = self.root / artifact_id
        try:
            metadata = json.loads((directory / "metadata.json").read_text("utf-8"))
            if metadata.get("artifact_id") != artifact_id:
                raise ValueError
            if not _HASH_RE.fullmatch(str(metadata.get("sha256") or "")):
                raise ValueError
            if self._clock() >= float(metadata.get("expires_at", 0)):
                raise BrowserArtifactError("artifact_expired")
            path = directory / "payload.bin"
            if not path.is_file() or path.is_symlink() or _is_reparse(path):
                raise ValueError
        except BrowserArtifactError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BrowserArtifactError("artifact_unavailable") from exc
        return path

    def inspect(self, artifact_id: str) -> dict:
        path = self.resolve_private_path(artifact_id)
        metadata = json.loads((path.parent / "metadata.json").read_text("utf-8"))
        return {key: metadata[key] for key in (
            "schema_version", "artifact_id", "size", "sha256", "mime_type",
            "classification", "created_at", "expires_at", "provenance",
            "private", "executable", "extractable",
        )}

    def cleanup(self) -> int:
        self._ensure_root()
        removed = 0
        now = self._clock()
        for item in list(self.root.iterdir()):
            if not item.is_dir():
                continue
            remove = item.name.startswith(".web_")
            if _ARTIFACT_RE.fullmatch(item.name):
                try:
                    metadata = json.loads((item / "metadata.json").read_text("utf-8"))
                    remove = now >= float(metadata.get("expires_at", 0))
                except Exception:
                    remove = True
            if remove:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        return removed


def operation_temp_dir(operation_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(operation_id or ""))[:96]
    if not safe:
        raise BrowserArtifactError("invalid_operation_id")
    root = Path(tempfile.gettempdir()) / "osia-browser-downloads"
    root.mkdir(parents=True, exist_ok=True)
    BrowserArtifactStore._chmod_private(root, directory=True)
    path = root / safe
    path.mkdir(exist_ok=False)
    BrowserArtifactStore._chmod_private(path, directory=True)
    return path


def cleanup_operation_temp(path: Path) -> None:
    root = (Path(tempfile.gettempdir()) / "osia-browser-downloads").resolve(strict=False)
    target = Path(path).resolve(strict=False)
    if target.parent == root:
        shutil.rmtree(target, ignore_errors=True)
    try:
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


_STORE = BrowserArtifactStore()


def get_browser_artifact_store() -> BrowserArtifactStore:
    return _STORE


def set_browser_artifact_store_for_testing(store: BrowserArtifactStore):
    global _STORE
    previous = _STORE
    _STORE = store
    return previous


__all__ = [
    "BROWSER_ARTIFACT_ROOT", "BrowserArtifactError", "BrowserArtifactStore",
    "MAX_DOWNLOAD_BYTES", "MAX_SESSION_TEMP_BYTES", "MAX_UPLOAD_BYTES",
    "ValidatedUpload", "cleanup_operation_temp", "detect_content",
    "get_browser_artifact_store", "operation_temp_dir", "revalidate_upload",
    "sanitize_suggested_filename", "set_browser_artifact_store_for_testing",
    "validate_upload_path",
]
