"""Opaque, owner-scoped document source capabilities for WOR-187.

The provider sees only ``src_*`` plus a safe format/state label.  Canonical
paths, source names and integrity digests remain in this private PC-only store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path

from osia.config import PRIVATE_DATA_DIR as _PRIVATE_DATA_DIR
from osia.document_identity import DocumentIdentityError, identify_document


SOURCE_ROOT = Path(_PRIVATE_DATA_DIR) / "document_sources"
SOURCE_TTL_SECONDS = 72 * 60 * 60
SOURCE_HARD_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCES = 64
MAX_METADATA_BYTES = 16 * 1024

_SOURCE_RE = re.compile(r"^src_[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SAFE_STATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DocumentSourceError(RuntimeError):
    def __init__(self, category: str, *, limit_kind: str | None = None):
        super().__init__(category)
        self.category = category
        self.limit_kind = limit_kind


def valid_source_ref(value) -> str:
    source_ref = str(value or "")
    if not _SOURCE_RE.fullmatch(source_ref):
        raise DocumentSourceError("invalid_source_reference")
    return source_ref


def owner_hash(owner_id: str) -> str:
    return hashlib.sha256(str(owner_id).encode("utf-8", errors="replace")).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        attrs = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & marker)


class DocumentSourceStore:
    def __init__(self, root: Path = SOURCE_ROOT, *, clock=None):
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

    @staticmethod
    def _read_snapshot(path: Path) -> tuple[bytes, os.stat_result]:
        try:
            if path.is_symlink() or _is_reparse(path):
                raise DocumentSourceError("unsupported_format")
            canonical = path.resolve(strict=True)
            with canonical.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise DocumentSourceError("unsupported_format")
                if before.st_size <= 0:
                    raise DocumentSourceError("corrupted_document")
                if before.st_size > MAX_SOURCE_BYTES:
                    raise DocumentSourceError(
                        "resource_limit_exceeded", limit_kind="file_size",
                    )
                data = handle.read(MAX_SOURCE_BYTES + 1)
                after = os.fstat(handle.fileno())
        except DocumentSourceError:
            raise
        except (OSError, RuntimeError) as exc:
            raise DocumentSourceError("source_unavailable") from exc
        if len(data) > MAX_SOURCE_BYTES:
            raise DocumentSourceError("resource_limit_exceeded", limit_kind="file_size")
        if (
            len(data) != before.st_size or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
            or (before.st_dev and after.st_dev and before.st_dev != after.st_dev)
        ):
            raise DocumentSourceError("source_changed")
        return data, before

    def create(
        self,
        path: str | Path,
        *,
        owner_id: str,
        source_kind: str,
        display_name: str = "",
        declared_mime: str = "",
        permission_epoch: int | None = None,
    ) -> dict:
        data, info = self._read_snapshot(Path(path).expanduser())
        return self._create_bytes(
            data,
            owner_digest=owner_hash(owner_id),
            source_kind=source_kind,
            private_name=Path(str(display_name or "")).name[:260],
            declared_mime=str(declared_mime or "")[:160],
            permission_epoch=permission_epoch,
            source_stat={
                "size": int(info.st_size),
                "modified_ns": int(info.st_mtime_ns),
                "device": int(info.st_dev),
                "inode": int(info.st_ino),
            },
        )

    def create_for_current_owner(
        self,
        path: str | Path,
        *,
        source_kind: str,
        display_name: str = "",
        declared_mime: str = "",
        permission_epoch: int | None = None,
    ) -> dict:
        from osia import document_artifacts

        data, info = self._read_snapshot(Path(path).expanduser())
        return self._create_bytes(
            data,
            owner_digest=document_artifacts.current_owner_hash(),
            source_kind=source_kind,
            private_name=Path(str(display_name or "")).name[:260],
            declared_mime=str(declared_mime or "")[:160],
            permission_epoch=permission_epoch,
            source_stat={
                "size": int(info.st_size), "modified_ns": int(info.st_mtime_ns),
                "device": int(info.st_dev), "inode": int(info.st_ino),
            },
        )

    def _create_bytes(
        self,
        data: bytes,
        *,
        owner_digest: str,
        source_kind: str,
        private_name: str,
        declared_mime: str,
        permission_epoch: int | None,
        source_stat: dict,
    ) -> dict:
        if not _HASH_RE.fullmatch(str(owner_digest or "")):
            raise DocumentSourceError("source_unavailable")
        kind = str(source_kind or "")
        if not _SAFE_KIND_RE.fullmatch(kind):
            raise DocumentSourceError("source_unavailable")
        identity = None
        failure = None
        limit_kind = None
        try:
            identity = identify_document(
                data, display_name=private_name, declared_mime=declared_mime,
            )
        except DocumentIdentityError as exc:
            failure = exc.category
            limit_kind = exc.limit_kind
        digest = hashlib.sha256(data).hexdigest()
        created_at = int(self._clock())
        source_ref = "src_" + secrets.token_hex(16)
        private = {
            "schema_version": 1,
            "source_ref": source_ref,
            "owner_hash": owner_digest,
            "source_hash": digest,
            "payload_bytes": len(data),
            "source_kind": kind,
            "private_filename": private_name,
            "declared_mime": declared_mime,
            "source_stat": dict(source_stat),
            "format_label": identity.format_label if identity else "unavailable",
            "document_family": identity.family if identity else "unknown",
            "warnings": list(identity.warnings) if identity else [],
            "failure_category": failure,
            "limit_kind": limit_kind,
            "permission_epoch": (
                int(permission_epoch) if type(permission_epoch) is int else None
            ),
            "bound_run_hash": "",
            "artifact_id": "",
            "artifact_policy_epoch": None,
            "created_at": created_at,
            "expires_at": min(
                created_at + SOURCE_TTL_SECONDS,
                created_at + SOURCE_HARD_TTL_SECONDS,
            ),
        }
        encoded = json.dumps(
            private, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise DocumentSourceError("resource_limit_exceeded", limit_kind="metadata_size")
        with self._lock:
            self._ensure_root()
            self.cleanup()
            directories = [item for item in self.root.iterdir() if item.is_dir() and _SOURCE_RE.fullmatch(item.name)]
            if len(directories) >= MAX_SOURCES:
                raise DocumentSourceError("resource_limit_exceeded", limit_kind="source_count")
            temporary = self.root / f".{source_ref}.{secrets.token_hex(8)}.tmp"
            final = self.root / source_ref
            try:
                temporary.mkdir()
                self._chmod_private(temporary, directory=True)
                with (temporary / "payload.bin").open("xb") as handle:
                    handle.write(data); handle.flush(); os.fsync(handle.fileno())
                with (temporary / "private.json").open("xb") as handle:
                    handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
                self._chmod_private(temporary / "payload.bin")
                self._chmod_private(temporary / "private.json")
                os.replace(temporary, final)
                self._chmod_private(final, directory=True)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return self._public(private)

    @staticmethod
    def _public(metadata: dict) -> dict:
        result = {
            "source_ref": metadata["source_ref"],
            "type_label": metadata.get("format_label", "unavailable"),
            "document_family": metadata.get("document_family", "unknown"),
            "state": "ready" if not metadata.get("failure_category") else "failed",
            "warnings": list(metadata.get("warnings") or [])[:8],
        }
        if metadata.get("failure_category"):
            result["category"] = metadata["failure_category"]
            if metadata.get("limit_kind"):
                result["limit_kind"] = metadata["limit_kind"]
        return result

    def resolve(self, source_ref: str) -> tuple[bytes, dict]:
        from osia import document_artifacts

        ref = valid_source_ref(source_ref)
        directory = self.root / ref
        try:
            raw = (directory / "private.json").read_bytes()
            if len(raw) > MAX_METADATA_BYTES:
                raise ValueError
            metadata = json.loads(raw.decode("utf-8"))
            if metadata.get("source_ref") != ref:
                raise ValueError
            if metadata.get("owner_hash") != document_artifacts.current_owner_hash():
                raise DocumentSourceError("unauthorized_source")
            if self._clock() >= float(metadata.get("expires_at", 0)):
                raise DocumentSourceError("expired_source")
            path = directory / "payload.bin"
            if path.is_symlink() or _is_reparse(path):
                raise ValueError
            data = path.read_bytes()
        except DocumentSourceError:
            raise
        except FileNotFoundError as exc:
            raise DocumentSourceError("missing_source") from exc
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise DocumentSourceError("corrupted_source") from exc
        if len(data) != int(metadata.get("payload_bytes", -1)):
            raise DocumentSourceError("corrupted_source")
        if hashlib.sha256(data).hexdigest() != metadata.get("source_hash"):
            raise DocumentSourceError("corrupted_source")
        if metadata.get("failure_category"):
            raise DocumentSourceError(
                str(metadata["failure_category"]),
                limit_kind=str(metadata.get("limit_kind") or "") or None,
            )
        try:
            identity = identify_document(
                data,
                display_name=str(metadata.get("private_filename") or ""),
                declared_mime=str(metadata.get("declared_mime") or ""),
            )
        except DocumentIdentityError as exc:
            raise DocumentSourceError(exc.category, limit_kind=exc.limit_kind) from exc
        if (
            identity.format_label != metadata.get("format_label")
            or identity.family != metadata.get("document_family")
        ):
            raise DocumentSourceError("source_changed")
        return data, metadata

    def _binding_metadata(
        self,
        source_refs,
        *,
        owner_id: str,
        permission_epoch: int,
    ) -> list[tuple[str, Path, dict]]:
        refs = [valid_source_ref(value) for value in source_refs]
        if len(refs) > 10:
            raise DocumentSourceError("resource_limit_exceeded", limit_kind="source_count")
        if len(set(refs)) != len(refs):
            raise DocumentSourceError("duplicate_source")
        expected_owner = owner_hash(owner_id)
        records = []
        for ref in refs:
            directory = self.root / ref
            try:
                raw = (directory / "private.json").read_bytes()
                if len(raw) > MAX_METADATA_BYTES:
                    raise ValueError
                metadata = json.loads(raw.decode("utf-8"))
            except FileNotFoundError as exc:
                raise DocumentSourceError("missing_source") from exc
            except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                raise DocumentSourceError("corrupted_source") from exc
            if metadata.get("source_ref") != ref:
                raise DocumentSourceError("corrupted_source")
            if metadata.get("owner_hash") != expected_owner:
                raise DocumentSourceError("unauthorized_source")
            if self._clock() >= float(metadata.get("expires_at", 0)):
                raise DocumentSourceError("expired_source")
            if metadata.get("failure_category"):
                raise DocumentSourceError("failed_source")
            if type(metadata.get("permission_epoch")) is not int or (
                metadata.get("permission_epoch") != permission_epoch
            ):
                raise DocumentSourceError("stale_permission_epoch")
            if metadata.get("bound_run_hash"):
                raise DocumentSourceError("source_already_bound")
            try:
                payload_path = directory / "payload.bin"
                if payload_path.is_symlink() or _is_reparse(payload_path):
                    raise ValueError
                data = payload_path.read_bytes()
                if len(data) != int(metadata.get("payload_bytes", -1)):
                    raise ValueError
                if hashlib.sha256(data).hexdigest() != metadata.get("source_hash"):
                    raise ValueError
                identity = identify_document(
                    data,
                    display_name=str(metadata.get("private_filename") or ""),
                    declared_mime=str(metadata.get("declared_mime") or ""),
                )
            except DocumentIdentityError as exc:
                raise DocumentSourceError(
                    exc.category, limit_kind=exc.limit_kind,
                ) from exc
            except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
                raise DocumentSourceError("corrupted_source") from exc
            if (
                identity.format_label != metadata.get("format_label")
                or identity.family != metadata.get("document_family")
            ):
                raise DocumentSourceError("source_changed")
            records.append((ref, directory, metadata))
        return records

    def inspect_for_binding(
        self,
        source_refs,
        *,
        owner_id: str,
        permission_epoch: int,
    ) -> list[dict]:
        """Validate exact session-owned sources without granting run access."""

        with self._lock:
            return [
                self._public(metadata)
                for _ref, _directory, metadata in self._binding_metadata(
                    source_refs,
                    owner_id=owner_id,
                    permission_epoch=permission_epoch,
                )
            ]

    @staticmethod
    def _encode_private(metadata: dict) -> bytes:
        encoded = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise DocumentSourceError("resource_limit_exceeded", limit_kind="metadata_size")
        return encoded

    def _replace_private(self, directory: Path, metadata: dict) -> None:
        encoded = self._encode_private(metadata)
        temporary = directory / (".private." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._chmod_private(temporary)
            os.replace(temporary, directory / "private.json")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def bind_for_run(
        self,
        source_refs,
        *,
        owner_id: str,
        run_owner_id: str,
        run_id: str,
        permission_epoch: int,
    ) -> list[dict]:
        """Atomically validate then bind session capabilities to one run owner."""

        with self._lock:
            records = self._binding_metadata(
                source_refs,
                owner_id=owner_id,
                permission_epoch=permission_epoch,
            )
            originals = [(directory, dict(metadata)) for _, directory, metadata in records]
            written = []
            try:
                for _ref, directory, metadata in records:
                    updated = dict(metadata)
                    updated["owner_hash"] = owner_hash(run_owner_id)
                    updated["bound_run_hash"] = owner_hash(run_id)
                    updated["bound_at"] = int(self._clock())
                    self._replace_private(directory, updated)
                    metadata.clear()
                    metadata.update(updated)
                    written.append(directory)
            except Exception:
                for directory, original in originals:
                    if directory in written:
                        try:
                            self._replace_private(directory, original)
                        except Exception:
                            pass
                raise DocumentSourceError("source_binding_failed")
            return [self._public(metadata) for _, _, metadata in records]

    def release_owned(self, source_refs, *, owner_id: str) -> int:
        """Delete only unbound sources owned by this authenticated session."""

        expected_owner = owner_hash(owner_id)
        refs = [valid_source_ref(value) for value in source_refs]
        if len(refs) > 10 or len(set(refs)) != len(refs):
            raise DocumentSourceError("duplicate_source")
        removed = 0
        with self._lock:
            for ref in refs:
                directory = self.root / ref
                try:
                    metadata = json.loads(
                        (directory / "private.json").read_text("utf-8")
                    )
                except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                if (
                    metadata.get("owner_hash") != expected_owner
                    or metadata.get("bound_run_hash")
                ):
                    raise DocumentSourceError("unauthorized_source")
                shutil.rmtree(directory, ignore_errors=True)
                removed += int(not directory.exists())
        return removed

    def link_artifact(
        self,
        source_ref: str,
        artifact_id: str,
        *,
        policy_epoch: int,
    ) -> None:
        """Retain one private source-to-artifact link for idempotent resume."""

        from osia import document_artifacts

        ref = valid_source_ref(source_ref)
        directory = self.root / ref
        with self._lock:
            try:
                metadata = json.loads(
                    (directory / "private.json").read_text("utf-8")
                )
            except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise DocumentSourceError("corrupted_source") from exc
            if metadata.get("owner_hash") != document_artifacts.current_owner_hash():
                raise DocumentSourceError("unauthorized_source")
            document_artifacts.valid_artifact_id(artifact_id)
            metadata["artifact_id"] = artifact_id
            metadata["artifact_policy_epoch"] = int(policy_epoch)
            self._replace_private(directory, metadata)

    def linked_artifacts(
        self,
        source_refs,
        *,
        run_owner_id: str,
        run_id: str,
        policy_epoch: int,
    ) -> list[str]:
        """Return linked artifact IDs only when every exact run source is ready."""

        expected_owner = owner_hash(run_owner_id)
        expected_run = owner_hash(run_id)
        artifacts = []
        with self._lock:
            for value in source_refs:
                ref = valid_source_ref(value)
                try:
                    metadata = json.loads(
                        ((self.root / ref) / "private.json").read_text("utf-8")
                    )
                except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise DocumentSourceError("missing_source") from exc
                artifact_id = str(metadata.get("artifact_id") or "")
                if (
                    metadata.get("owner_hash") != expected_owner
                    or metadata.get("bound_run_hash") != expected_run
                    or metadata.get("artifact_policy_epoch") != policy_epoch
                    or not artifact_id
                ):
                    raise DocumentSourceError("source_not_processed")
                artifacts.append(artifact_id)
        return artifacts

    def release_bound_run(
        self,
        source_refs,
        *,
        run_owner_id: str,
        run_id: str,
    ) -> int:
        """Delete only sources sealed to this exact terminal run."""

        expected_owner = owner_hash(run_owner_id)
        expected_run = owner_hash(run_id)
        removed = 0
        with self._lock:
            for value in source_refs:
                try:
                    ref = valid_source_ref(value)
                    directory = self.root / ref
                    metadata = json.loads(
                        (directory / "private.json").read_text("utf-8")
                    )
                except (DocumentSourceError, FileNotFoundError, OSError, ValueError,
                        TypeError, json.JSONDecodeError):
                    continue
                if (
                    metadata.get("owner_hash") != expected_owner
                    or metadata.get("bound_run_hash") != expected_run
                ):
                    continue
                shutil.rmtree(directory, ignore_errors=True)
                removed += int(not directory.exists())
        return removed

    def cleanup(self) -> int:
        self._ensure_root()
        removed = 0
        now = self._clock()
        for item in list(self.root.iterdir()):
            if not item.is_dir():
                continue
            remove = item.name.startswith(".src_")
            if _SOURCE_RE.fullmatch(item.name):
                try:
                    raw = (item / "private.json").read_bytes()
                    if len(raw) > MAX_METADATA_BYTES:
                        raise ValueError
                    metadata = json.loads(raw.decode("utf-8"))
                    remove = now >= float(metadata.get("expires_at", 0))
                except Exception:
                    remove = True
            if remove:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
        return removed

    def delete(self, source_ref: str) -> bool:
        try:
            ref = valid_source_ref(source_ref)
        except DocumentSourceError:
            return False
        directory = self.root / ref
        if directory.parent.resolve(strict=False) != self.root.resolve(strict=False):
            return False
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            return not directory.exists()
        return False


_STORE = DocumentSourceStore()


def get_source_store() -> DocumentSourceStore:
    return _STORE


def set_source_store_for_testing(store: DocumentSourceStore):
    global _STORE
    previous = _STORE
    _STORE = store
    return previous


__all__ = [
    "DocumentSourceError", "DocumentSourceStore", "MAX_SOURCE_BYTES",
    "SOURCE_ROOT", "get_source_store", "owner_hash", "set_source_store_for_testing",
    "valid_source_ref",
]
