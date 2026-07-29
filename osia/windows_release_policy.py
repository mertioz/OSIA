"""Narrow safety policy for external files copied into Windows releases."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence, TypeVar


_GENERATED_DATA_SUFFIXES = {".pyc", ".pyo"}
_GENERATED_DATA_NAMES = {"direct_url.json"}
_TocEntry = TypeVar("_TocEntry", bound=Sequence[object])


def is_generated_data_path(value: str | os.PathLike[str]) -> bool:
    """Return whether a copied-data destination is generated Python cache data."""

    normalized = str(value).replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        any(part.casefold() == "__pycache__" for part in path.parts)
        or path.name.casefold() in _GENERATED_DATA_NAMES
        or path.suffix.casefold() in _GENERATED_DATA_SUFFIXES
    )


def filter_pyinstaller_data_toc(
    entries: Iterable[_TocEntry],
) -> tuple[list[_TocEntry], tuple[str, ...]]:
    """Filter generated cache files from PyInstaller's external DATA table.

    PyInstaller keeps importable modules in a separate PURE table/PYZ.  Taking
    only the DATA table here makes the exclusion incapable of removing normal
    frozen Python modules or native binaries.
    """

    kept: list[_TocEntry] = []
    removed: list[str] = []
    for entry in entries:
        destination = str(entry[0])
        if is_generated_data_path(destination):
            removed.append(destination.replace("\\", "/"))
        else:
            kept.append(entry)
    return kept, tuple(sorted(removed))


def _private_markers(
    forbidden_roots: Iterable[str | os.PathLike[str]],
) -> tuple[bytes, ...]:
    variants: set[str] = set()
    for raw in forbidden_roots:
        value = os.fspath(raw).rstrip("/\\")
        if len(value) < 4:
            continue
        variants.update({
            value,
            value.replace("\\", "/"),
            value.replace("/", "\\"),
        })
    encoded: set[bytes] = set()
    for value in variants:
        encoded.add(value.encode("utf-8"))
        encoded.add(value.encode("utf-16-le"))
    return tuple(sorted(encoded, key=len, reverse=True))


def scan_release_payload(
    root: Path,
    *,
    forbidden_roots: Iterable[str | os.PathLike[str]],
) -> tuple[int, int]:
    """Reject loose cache files and private build roots in a release payload.

    The scan treats archives and native binaries as opaque bytes.  It therefore
    validates copied data plus raw installer bytes without unpacking or altering
    PyInstaller's internal PYZ architecture.
    """

    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(root)
    if root.is_dir():
        for directory in root.rglob("*"):
            if directory.is_dir() and directory.name.casefold() == "__pycache__":
                relative = directory.relative_to(root).as_posix()
                raise RuntimeError(
                    f"generated cache directory in release payload: {relative}"
                )
    files = [root] if root.is_file() else sorted(
        path for path in root.rglob("*") if path.is_file()
    )
    markers = _private_markers(forbidden_roots)
    overlap = max((len(marker) for marker in markers), default=1) - 1
    total_bytes = 0

    for path in files:
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        if is_generated_data_path(relative):
            raise RuntimeError(f"generated cache file in release payload: {relative}")
        total_bytes += path.stat().st_size
        if not markers:
            continue
        tail = b""
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                data = (tail + chunk).lower()
                if any(marker.lower() in data for marker in markers):
                    raise RuntimeError(
                        f"private build path in release payload file: {relative}"
                    )
                tail = data[-overlap:] if overlap else b""
    return len(files), total_bytes


__all__ = [
    "filter_pyinstaller_data_toc",
    "is_generated_data_path",
    "scan_release_payload",
]
