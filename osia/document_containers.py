"""Reusable bounded ZIP and XML inspection for passive document parsing.

Untrusted containers are never extracted.  Every member is validated and read
through :mod:`zipfile` so CRC/truncation failures are observed before an
extractor sees any package part.
"""

from __future__ import annotations

import io
import posixpath
import re
import stat
import struct
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


MAX_ZIP_MEMBERS = 5_000
MAX_ZIP_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_NON_XML_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XML_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBER_RATIO = 100.0
MAX_AGGREGATE_RATIO = 50.0
MAX_XML_DEPTH = 128
MAX_XML_ATTRIBUTES = 256
MAX_XML_NAME_LENGTH = 256
MAX_XML_NAMESPACES = 64
MAX_NAMESPACE_URI_LENGTH = 512
MAX_RELATIONSHIP_DEPTH = 32
MAX_RELATIONSHIP_EDGES = 50_000

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_XML_SUFFIXES = (".xml", ".rels", ".xhtml", ".html", ".opf", ".ncx")
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_ALLOWED_COMPRESSION = frozenset({
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
    zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA,
})


class ContainerError(RuntimeError):
    """Path-free container/XML failure."""

    def __init__(self, category: str, *, limit_kind: str | None = None):
        super().__init__(category)
        self.category = category
        self.limit_kind = limit_kind


@dataclass(frozen=True)
class ZipMember:
    name: str
    size: int
    compressed_size: int
    compression: int
    xml: bool


def local_name(value: str) -> str:
    return str(value or "").rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def namespace_uri(value: str) -> str:
    text = str(value or "")
    if text.startswith("{") and "}" in text:
        return text[1:text.index("}")]
    return ""


def _safe_member_name(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ContainerError("corrupted_document")
    if "\\" in raw:
        # ZIP packages use forward slashes.  Accepting both creates ambiguous
        # traversal and duplicate-name semantics across operating systems.
        raise ContainerError("corrupted_document")
    if raw.startswith(("/", "//")) or "//" in raw or _DRIVE_RE.match(raw):
        raise ContainerError("corrupted_document")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ContainerError("corrupted_document")
    normalized = posixpath.normpath(raw)
    if normalized != raw.rstrip("/"):
        raise ContainerError("corrupted_document")
    return raw.rstrip("/")


def _member_limit(name: str) -> int:
    return MAX_XML_MEMBER_BYTES if name.lower().endswith(_XML_SUFFIXES) else MAX_NON_XML_MEMBER_BYTES


def _validate_local_header(data: bytes, info: zipfile.ZipInfo) -> None:
    """Cross-check local headers against the central directory."""

    offset = int(info.header_offset)
    if offset < 0 or offset + 30 > len(data):
        raise ContainerError("corrupted_document")
    try:
        fields = struct.unpack_from("<IHHHHHIIIHH", data, offset)
    except struct.error as exc:
        raise ContainerError("corrupted_document") from exc
    signature, flags, compression = fields[0], fields[2], fields[3]
    crc, compressed, size = fields[6], fields[7], fields[8]
    name_length, extra_length = fields[9], fields[10]
    if signature != 0x04034B50:
        raise ContainerError("corrupted_document")
    end = offset + 30 + name_length + extra_length
    if end > len(data):
        raise ContainerError("corrupted_document")
    raw_name = data[offset + 30: offset + 30 + name_length]
    try:
        decoded = raw_name.decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeDecodeError as exc:
        raise ContainerError("corrupted_document") from exc
    if decoded != info.orig_filename:
        raise ContainerError("corrupted_document")
    if flags != int(info.flag_bits) or compression != int(info.compress_type):
        raise ContainerError("corrupted_document")
    # Bit 3 means the authoritative sizes/CRC follow the payload in a data
    # descriptor, so zero local fields are valid in that case.
    if not (flags & 0x08) and (
        crc != info.CRC or compressed != info.compress_size or size != info.file_size
    ):
        raise ContainerError("corrupted_document")


class SafeZip:
    """Fully preinspected, bounded, read-only in-memory ZIP view."""

    def __init__(self, data: bytes):
        self._data = bytes(data)
        self._buffer = io.BytesIO(self._data)
        try:
            self._zip = zipfile.ZipFile(self._buffer, "r")
            infos = self._zip.infolist()
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ContainerError("corrupted_document") from exc
        if not infos:
            self.close()
            raise ContainerError("corrupted_document")
        if len(infos) > MAX_ZIP_MEMBERS:
            self.close()
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="zip_member_count",
            )
        self._infos: dict[str, zipfile.ZipInfo] = {}
        self.members: list[ZipMember] = []
        exact: set[str] = set()
        normalized: set[str] = set()
        casefolded: set[str] = set()
        total_size = 0
        total_compressed = 0
        try:
            for info in infos:
                raw = info.orig_filename
                name = _safe_member_name(raw)
                if raw.endswith("/"):
                    # Directory entries carry no parser-visible content but
                    # still participate in duplicate/collision checks.
                    name = raw.rstrip("/")
                if not name:
                    raise ContainerError("corrupted_document")
                unicode_key = unicodedata.normalize("NFC", name)
                fold_key = unicode_key.casefold()
                if name in exact or unicode_key in normalized or fold_key in casefolded:
                    raise ContainerError("corrupted_document")
                exact.add(name)
                normalized.add(unicode_key)
                casefolded.add(fold_key)
                mode = (int(info.external_attr) >> 16) & 0xFFFF
                dos_attributes = int(info.external_attr) & 0xFFFF
                if stat.S_ISLNK(mode) or (dos_attributes & 0x0400):
                    raise ContainerError("corrupted_document")
                if mode and not (
                    stat.S_ISREG(mode) or stat.S_ISDIR(mode) or (mode & 0o170000) == 0
                ):
                    raise ContainerError("corrupted_document")
                if int(info.flag_bits) & 0x01:
                    raise ContainerError("encrypted_document")
                if info.compress_type not in _ALLOWED_COMPRESSION:
                    raise ContainerError("corrupted_document")
                if info.file_size < 0 or info.compress_size < 0:
                    raise ContainerError("corrupted_document")
                limit = _member_limit(name)
                if info.file_size > limit:
                    raise ContainerError(
                        "resource_limit_exceeded",
                        limit_kind="xml_part_size" if limit == MAX_XML_MEMBER_BYTES else "zip_member_size",
                    )
                ratio = (
                    float(info.file_size) / float(info.compress_size)
                    if info.compress_size else (float("inf") if info.file_size else 1.0)
                )
                if ratio > MAX_MEMBER_RATIO:
                    raise ContainerError(
                        "resource_limit_exceeded", limit_kind="zip_member_ratio",
                    )
                total_size += int(info.file_size)
                total_compressed += int(info.compress_size)
                if total_size > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise ContainerError(
                        "resource_limit_exceeded", limit_kind="zip_uncompressed_size",
                    )
                _validate_local_header(self._data, info)
                if not raw.endswith("/"):
                    self._infos[name] = info
                    self.members.append(ZipMember(
                        name=name,
                        size=int(info.file_size),
                        compressed_size=int(info.compress_size),
                        compression=int(info.compress_type),
                        xml=name.lower().endswith(_XML_SUFFIXES),
                    ))
            aggregate_ratio = (
                float(total_size) / float(total_compressed)
                if total_compressed else (float("inf") if total_size else 1.0)
            )
            if aggregate_ratio > MAX_AGGREGATE_RATIO:
                raise ContainerError(
                    "resource_limit_exceeded", limit_kind="zip_aggregate_ratio",
                )
            # Read every member now: this proves CRC consistency and truncation
            # before format identity or extraction can report success.
            for member in self.members:
                prefix = self._validate_member(member)
                if prefix.startswith(_ZIP_SIGNATURES):
                    raise ContainerError(
                        "resource_limit_exceeded", limit_kind="nested_archive",
                    )
        except Exception:
            self.close()
            raise

    def _validate_member(self, member: ZipMember) -> bytes:
        total = 0
        prefix = b""
        try:
            with self._zip.open(self._infos[member.name], "r") as handle:
                while True:
                    block = handle.read(min(1024 * 1024, _member_limit(member.name) + 1 - total))
                    if not block:
                        break
                    if not prefix:
                        prefix = block[:8]
                    total += len(block)
                    if total > _member_limit(member.name):
                        raise ContainerError(
                            "resource_limit_exceeded", limit_kind="zip_member_size",
                        )
        except ContainerError:
            raise
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ContainerError("corrupted_document") from exc
        if total != member.size:
            raise ContainerError("corrupted_document")
        return prefix

    def close(self) -> None:
        try:
            self._zip.close()
        except Exception:
            pass
        try:
            self._buffer.close()
        except Exception:
            pass

    def __enter__(self) -> "SafeZip":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def names(self) -> tuple[str, ...]:
        return tuple(member.name for member in self.members)

    def has(self, name: str) -> bool:
        return str(name) in self._infos

    def info(self, name: str) -> ZipMember:
        key = str(name)
        info = self._infos.get(key)
        if info is None:
            raise ContainerError("corrupted_document")
        return ZipMember(
            key, int(info.file_size), int(info.compress_size),
            int(info.compress_type), key.lower().endswith(_XML_SUFFIXES),
        )

    def read(self, name: str, *, limit: int | None = None) -> bytes:
        key = str(name)
        info = self._infos.get(key)
        if info is None:
            raise ContainerError("corrupted_document")
        ceiling = min(_member_limit(key), int(limit or _member_limit(key)))
        if info.file_size > ceiling:
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="xml_part_size" if key.lower().endswith(_XML_SUFFIXES) else "zip_member_size",
            )
        try:
            with self._zip.open(info, "r") as handle:
                value = handle.read(ceiling + 1)
                if handle.read(1):
                    raise ContainerError(
                        "resource_limit_exceeded", limit_kind="zip_member_size",
                    )
        except ContainerError:
            raise
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ContainerError("corrupted_document") from exc
        if len(value) != info.file_size:
            raise ContainerError("corrupted_document")
        return value

    def prefix(self, name: str, length: int = 16) -> bytes:
        key = str(name)
        info = self._infos.get(key)
        if info is None or isinstance(length, bool) or not (1 <= int(length) <= 512):
            raise ContainerError("corrupted_document")
        try:
            with self._zip.open(info, "r") as handle:
                return handle.read(int(length))
        except (OSError, EOFError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ContainerError("corrupted_document") from exc

    def xml(self, name: str) -> ET.Element:
        return safe_xml_from_bytes(self.read(name, limit=MAX_XML_MEMBER_BYTES))


def safe_xml_from_bytes(data: bytes) -> ET.Element:
    """Parse XML after rejecting entity/DTD facilities and enforcing bounds."""

    if len(data) > MAX_XML_MEMBER_BYTES:
        raise ContainerError(
            "resource_limit_exceeded", limit_kind="xml_part_size",
        )
    upper = bytes(data).upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ContainerError("active_content_detected")
    namespace_declarations = re.findall(
        rb"\bxmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?\s*=\s*(['\"])(.*?)\1",
        data,
    )
    if len(namespace_declarations) > MAX_XML_NAMESPACES:
        raise ContainerError(
            "resource_limit_exceeded", limit_kind="xml_namespace_count",
        )
    namespace_values = {value for _quote, value in namespace_declarations}
    if any(len(value) > MAX_NAMESPACE_URI_LENGTH for value in namespace_values):
        raise ContainerError(
            "resource_limit_exceeded", limit_kind="namespace_uri_length",
        )
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError, UnicodeError) as exc:
        raise ContainerError("corrupted_document") from exc
    namespaces: set[str] = set()
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if depth > MAX_XML_DEPTH:
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="xml_depth",
            )
        if not isinstance(element.tag, str):
            continue
        if len(local_name(element.tag)) > MAX_XML_NAME_LENGTH:
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="xml_name_length",
            )
        uri = namespace_uri(element.tag)
        if uri:
            if len(uri) > MAX_NAMESPACE_URI_LENGTH:
                raise ContainerError(
                    "resource_limit_exceeded", limit_kind="namespace_uri_length",
                )
            namespaces.add(uri)
        if len(element.attrib) > MAX_XML_ATTRIBUTES:
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="xml_attribute_count",
            )
        for key in element.attrib:
            if len(local_name(key)) > MAX_XML_NAME_LENGTH:
                raise ContainerError(
                    "resource_limit_exceeded", limit_kind="xml_name_length",
                )
            attr_uri = namespace_uri(key)
            if attr_uri:
                if len(attr_uri) > MAX_NAMESPACE_URI_LENGTH:
                    raise ContainerError(
                        "resource_limit_exceeded", limit_kind="namespace_uri_length",
                    )
                namespaces.add(attr_uri)
        if len(namespaces) > MAX_XML_NAMESPACES:
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="xml_namespace_count",
            )
        stack.extend((child, depth + 1) for child in reversed(list(element)))
    return root


def relationship_source(part_name: str) -> str:
    """Return the package part that owns a ``*.rels`` member."""

    name = str(part_name)
    if name == "_rels/.rels":
        return ""
    if name.startswith("_rels/") and name.endswith(".rels"):
        leaf = name[len("_rels/"):]
        if "/" not in leaf and leaf != ".rels":
            return leaf[:-5]
    marker = "/_rels/"
    if marker not in name or not name.endswith(".rels"):
        raise ContainerError("corrupted_document")
    prefix, leaf = name.rsplit(marker, 1)
    return posixpath.join(prefix, leaf[:-5])


def resolve_relationship_target(source: str, target: str) -> str:
    raw = str(target or "")
    if not raw or "\x00" in raw or "\\" in raw:
        raise ContainerError("corrupted_document")
    if raw.startswith("/"):
        candidate = raw.lstrip("/")
    else:
        candidate = posixpath.join(posixpath.dirname(source), raw)
    normalized = posixpath.normpath(candidate)
    if normalized == ".." or normalized.startswith("../") or _DRIVE_RE.match(normalized):
        raise ContainerError("corrupted_document")
    return normalized


def inspect_relationships(container: SafeZip) -> list[dict]:
    """Validate relationship IDs/targets and the bounded internal graph."""

    relationships: list[dict] = []
    graph: dict[str, set[str]] = {}
    for name in container.names():
        if not name.endswith(".rels"):
            continue
        source = relationship_source(name)
        root = container.xml(name)
        seen_ids: set[str] = set()
        for element in root.iter():
            if local_name(element.tag) != "Relationship":
                continue
            rel_id = str(element.attrib.get("Id") or "")
            target = str(element.attrib.get("Target") or "")
            rel_type = str(element.attrib.get("Type") or "")
            mode = str(element.attrib.get("TargetMode") or "Internal")
            if not rel_id or rel_id in seen_ids or not target or len(target) > 4_096:
                raise ContainerError("corrupted_document")
            seen_ids.add(rel_id)
            if len(relationships) >= MAX_RELATIONSHIP_EDGES:
                raise ContainerError(
                    "resource_limit_exceeded", limit_kind="relationship_edges",
                )
            external = mode.casefold() == "external"
            resolved = "" if external else resolve_relationship_target(source, target)
            if not external and not container.has(resolved):
                # Some producers use relationships to optional missing targets;
                # fail closed rather than silently dropping structure.
                raise ContainerError("corrupted_document")
            record = {
                "source": source,
                "id": rel_id,
                "type": rel_type,
                "target": target if external else resolved,
                "external": external,
            }
            relationships.append(record)
            if not external:
                relation_leaf = rel_type.rsplit("/", 1)[-1].casefold()
                # Standard OOXML packages intentionally contain structural
                # back-links (for example notesSlide -> slide and
                # slideLayout <-> slideMaster). They are not extraction
                # traversal edges. Keep cycle checks for package-navigation
                # and unknown relationships while excluding those known
                # metadata/back-reference classes.
                if relation_leaf not in {
                    "hyperlink", "image", "audio", "video", "theme",
                    "slidelayout", "slidemaster", "notesmaster",
                    "notesslide", "handoutmaster", "styles", "numbering",
                    "fonttable", "settings", "websettings", "customxml",
                    "vmldrawing", "drawing", "chart", "comments", "table",
                    "sharedstrings", "calcchain", "printersettings",
                }:
                    graph.setdefault(source, set()).add(resolved)
    # Cycle and depth detection over package-part relationships.
    def visit(node: str, path: tuple[str, ...]) -> None:
        if len(path) > MAX_RELATIONSHIP_DEPTH:
            raise ContainerError(
                "resource_limit_exceeded", limit_kind="relationship_depth",
            )
        for child in sorted(graph.get(node, ())):
            if child in path:
                raise ContainerError("corrupted_document")
            visit(child, path + (child,))

    for root in sorted(graph):
        visit(root, (root,))
    return relationships


__all__ = [
    "ContainerError", "MAX_RELATIONSHIP_EDGES", "MAX_XML_MEMBER_BYTES",
    "SafeZip", "ZipMember", "inspect_relationships", "local_name",
    "namespace_uri", "resolve_relationship_target", "safe_xml_from_bytes",
]
