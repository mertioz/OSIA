"""Authoritative byte-first document identity and passive-content policy."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from osia.document_containers import (
    ContainerError,
    SafeZip,
    inspect_relationships,
    local_name,
    resolve_relationship_target,
)


SUPPORTED_FORMATS = frozenset({
    "pdf", "png", "jpeg", "docx", "odt", "rtf", "txt", "markdown",
    "html", "epub", "xlsx", "ods", "csv", "tsv", "pptx", "odp",
})

_EXECUTABLE_PREFIXES = (
    b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
)
_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_RTF_RE = re.compile(rb"^\s*\{\\rtf(?:1|\d+)\b", re.I)
_REMOTE_SCHEMES = frozenset({"http", "https", "ftp", "ftps", "file", "smb", "data", "javascript"})
_KNOWN_EXTENSIONS = {
    ".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg",
    ".docx": "docx", ".docm": "docx", ".odt": "odt", ".rtf": "rtf",
    ".txt": "txt", ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html", ".xhtml": "html", ".epub": "epub",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls", ".ods": "ods",
    ".csv": "csv", ".tsv": "tsv", ".pptx": "pptx", ".pptm": "pptx",
    ".ppt": "ppt", ".odp": "odp",
}
_FORMAT_MIMES = {
    "pdf": {"application/pdf"},
    "png": {"image/png"},
    "jpeg": {"image/jpeg"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "odt": {"application/vnd.oasis.opendocument.text"},
    "rtf": {"application/rtf", "text/rtf"},
    "txt": {"text/plain"},
    "markdown": {"text/markdown", "text/x-markdown"},
    "html": {"text/html", "application/xhtml+xml"},
    "epub": {"application/epub+zip"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "ods": {"application/vnd.oasis.opendocument.spreadsheet"},
    "csv": {"text/csv"},
    "tsv": {"text/tab-separated-values"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "odp": {"application/vnd.oasis.opendocument.presentation"},
}
_FAMILIES = {
    "pdf": "document", "png": "image", "jpeg": "image", "docx": "document",
    "odt": "document", "rtf": "document", "txt": "document",
    "markdown": "document", "html": "document", "epub": "document",
    "xlsx": "spreadsheet", "ods": "spreadsheet", "csv": "spreadsheet",
    "tsv": "spreadsheet", "pptx": "presentation", "odp": "presentation",
}


class DocumentIdentityError(RuntimeError):
    """One stable byte-identity or passive-policy rejection."""

    def __init__(self, category: str, *, limit_kind: str | None = None):
        super().__init__(category)
        self.category = category
        self.limit_kind = limit_kind


@dataclass(frozen=True)
class DocumentIdentity:
    format_label: str
    family: str
    warnings: tuple[str, ...] = ()

    def public(self) -> dict:
        return {
            "format_label": self.format_label,
            "document_family": self.family,
            "warnings": list(self.warnings),
        }


def _raise_container(exc: ContainerError):
    raise DocumentIdentityError(exc.category, limit_kind=exc.limit_kind) from exc


def _is_remote(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.casefold() in _REMOTE_SCHEMES or str(value).startswith("//")


def _decode_text(data: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if data.startswith(b"\xef\xbb\xbf"):
        codec = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        codec = "utf-16"
    else:
        codec = "utf-8"
    try:
        text = data.decode(codec, errors="strict")
    except UnicodeDecodeError:
        if codec == "utf-8" and b"\x00" in data:
            raise DocumentIdentityError("unsupported_format")
        text = data.decode(codec, errors="replace")
        warnings.append("decoding_replacements")
    if "\x00" in text:
        raise DocumentIdentityError("unsupported_format")
    controls = sum(ord(ch) < 32 and ch not in "\t\n\r" for ch in text[:100_000])
    if controls > max(8, len(text[:100_000]) // 100):
        raise DocumentIdentityError("unsupported_format")
    return text, warnings


class _HTMLPolicyParser(HTMLParser):
    """Inspect tags/attributes without executing or resolving resources."""

    _ACTIVE_TAGS = frozenset({"script", "iframe", "object", "embed"})
    _RESOURCE_TAGS = frozenset({
        "img", "link", "source", "video", "audio", "track", "input",
        "base", "image", "use",
    })

    def __init__(self, *, allow_local_resources: bool = False):
        super().__init__(convert_charrefs=True)
        self.allow_local_resources = bool(allow_local_resources)

    def handle_starttag(self, tag, attrs):
        name = str(tag or "").casefold()
        if name in self._ACTIVE_TAGS:
            raise DocumentIdentityError("active_content_detected")
        values = {
            str(key or "").casefold(): str(value or "").strip()
            for key, value in attrs
        }
        if any(key.startswith("on") for key in values):
            raise DocumentIdentityError("active_content_detected")
        for key in ("href", "src", "xlink:href"):
            target = values.get(key, "")
            scheme = urlparse(target).scheme.casefold()
            if scheme in {"javascript", "data"}:
                raise DocumentIdentityError("active_content_detected")
            if name in self._RESOURCE_TAGS and target and not target.startswith("#"):
                if _is_remote(target) or not self.allow_local_resources:
                    raise DocumentIdentityError("external_reference_detected")
        if (
            name == "meta"
            and values.get("http-equiv", "").casefold() == "refresh"
            and re.search(r"\burl\s*=", values.get("content", ""), re.I)
        ):
            raise DocumentIdentityError("external_reference_detected")


def _inspect_html_policy(text: str, *, allow_local_resources: bool = False) -> None:
    parser = _HTMLPolicyParser(allow_local_resources=allow_local_resources)
    try:
        parser.feed(text)
        parser.close()
    except DocumentIdentityError:
        raise
    except Exception as exc:
        raise DocumentIdentityError("corrupted_document") from exc
    if re.search(r"@import\s+(?:url\()?\s*['\"]?(?:https?:)?//", text, re.I):
        raise DocumentIdentityError("external_reference_detected")
    if re.search(r"url\(\s*['\"]?(?:https?:)?//", text, re.I):
        raise DocumentIdentityError("external_reference_detected")


def _inspect_rtf(data: bytes) -> None:
    lower = data.lower()
    active = (
        rb"\\object\b", rb"\\objdata\b", rb"\\bin\d*\b", rb"\\dde(?:auto)?\b",
        rb"\\include(?:picture|text)\b", rb"\\package\b",
    )
    if any(re.search(pattern, lower) for pattern in active):
        raise DocumentIdentityError("active_content_detected")
    if re.search(rb"\\fldinst\b[^{}]{0,4096}(?:dde|include|link|import)", lower):
        raise DocumentIdentityError("external_reference_detected")


def _relationship_policy(relationships: list[dict]) -> None:
    for relationship in relationships:
        rel_type = str(relationship.get("type") or "").casefold()
        if not relationship.get("external"):
            continue
        target = str(relationship.get("target") or "").strip()
        scheme = urlparse(target).scheme.casefold()
        if scheme in {"javascript", "data"}:
            raise DocumentIdentityError("active_content_detected")
        if not rel_type.endswith("/hyperlink") or scheme in {"file", "smb"}:
            raise DocumentIdentityError("external_reference_detected")


def _reject_embedded_executables(container: SafeZip) -> None:
    for member in container.members:
        if member.xml:
            continue
        prefix = container.prefix(member.name, 16)
        if prefix.startswith(_EXECUTABLE_PREFIXES) or prefix.startswith(b"#!"):
            raise DocumentIdentityError("active_content_detected")


def _xml_package_policy(container: SafeZip, format_label: str) -> None:
    names = container.names()
    lowered = {name.casefold(): name for name in names}
    active_name_parts = (
        "vbaproject", "/activex/", "/embeddings/", "oleobject", "customui/",
    )
    if any(any(part in f"/{name.casefold()}" for part in active_name_parts) for name in names):
        raise DocumentIdentityError("active_content_detected")
    if any(name.casefold().endswith((".exe", ".dll", ".com", ".bat", ".cmd", ".ps1", ".js", ".vbs")) for name in names):
        raise DocumentIdentityError("active_content_detected")
    _reject_embedded_executables(container)
    if format_label == "xlsx" and any(
        marker in lowered for marker in (
            "xl/connections.xml", "xl/externalconnections.xml",
        )
    ):
        raise DocumentIdentityError("external_reference_detected")
    if format_label == "xlsx" and any(name.startswith("xl/externalLinks/") for name in names):
        raise DocumentIdentityError("external_reference_detected")
    for name in names:
        lower = name.casefold()
        if not lower.endswith((".xml", ".rels", ".xhtml", ".html", ".opf")):
            continue
        raw = container.read(name)
        if lower.endswith((".xml", ".rels", ".xhtml", ".opf")):
            container.xml(name)
        if lower.endswith((".html", ".xhtml")):
            _inspect_html_policy(raw.decode("utf-8", errors="replace"))
        folded = raw.lower()
        if b"ddeauto" in folded or b"<w:instrtext" in folded and re.search(
            rb"(?:dde|include|link)\b", folded,
        ):
            raise DocumentIdentityError("active_content_detected")
        if b"ppaction://" in folded or b"<script" in folded:
            raise DocumentIdentityError("active_content_detected")
        if re.search(rb"\son[a-z0-9_-]+\s*=", folded):
            raise DocumentIdentityError("active_content_detected")
        if b"documentprotection" in folded or b"workbookprotection" in folded or b"sheetprotection" in folded:
            raise DocumentIdentityError("protected_document")
    try:
        _relationship_policy(inspect_relationships(container))
    except ContainerError as exc:
        _raise_container(exc)


def _odf_policy(container: SafeZip, format_label: str) -> None:
    names = container.names()
    _reject_embedded_executables(container)
    if container.has("META-INF/manifest.xml"):
        raw = container.read("META-INF/manifest.xml")
        if b"encryption-data" in raw.lower():
            raise DocumentIdentityError("encrypted_document")
    for name in names:
        lower = name.casefold()
        if lower.startswith(("scripts/", "basic/")) or "/scripts/" in lower:
            raise DocumentIdentityError("active_content_detected")
        if lower.endswith((".exe", ".dll", ".com", ".bat", ".cmd", ".js")):
            raise DocumentIdentityError("active_content_detected")
        if not lower.endswith((".xml", ".xhtml", ".html")):
            continue
        root = container.xml(name)
        for element in root.iter():
            tag = local_name(element.tag).casefold()
            attrs = {local_name(k).casefold(): str(v) for k, v in element.attrib.items()}
            if tag in {"scripts", "script", "event-listener", "object-ole"}:
                raise DocumentIdentityError("active_content_detected")
            if any(key.startswith("on") for key in attrs):
                raise DocumentIdentityError("active_content_detected")
            href = attrs.get("href", "")
            if urlparse(href).scheme.casefold() in {"javascript", "data"}:
                raise DocumentIdentityError("active_content_detected")
            if href and _is_remote(href) and tag not in {"a"}:
                raise DocumentIdentityError("external_reference_detected")
            if tag in {"object", "image", "fill-image", "font-face-uri"} and _is_remote(href):
                raise DocumentIdentityError("external_reference_detected")


def _epub_policy(container: SafeZip) -> None:
    if container.has("META-INF/encryption.xml") or container.has("META-INF/rights.xml"):
        raise DocumentIdentityError("drm_protected")
    names = container.names()
    _reject_embedded_executables(container)
    for name in names:
        lower = name.casefold()
        if lower.endswith((".js", ".mjs")):
            raise DocumentIdentityError("active_content_detected")
        if lower.endswith((".xhtml", ".html", ".htm", ".svg", ".css")):
            try:
                text = container.read(name).decode("utf-8", errors="replace")
            except UnicodeError as exc:
                raise DocumentIdentityError("corrupted_document") from exc
            _inspect_html_policy(text, allow_local_resources=True)
            if lower.endswith((".xhtml", ".svg")):
                container.xml(name)
    # OPF manifest resources may be remote even when no local XHTML references
    # them directly.
    for name in names:
        if name.casefold().endswith(".opf"):
            root = container.xml(name)
            for element in root.iter():
                if local_name(element.tag).casefold() == "item":
                    href = str(element.attrib.get("href") or "")
                    if _is_remote(href):
                        raise DocumentIdentityError("external_reference_detected")
                    target = resolve_relationship_target(name, unquote(href).split("#", 1)[0])
                    if not container.has(target):
                        raise DocumentIdentityError("corrupted_document")


def _zip_identity(data: bytes) -> str:
    try:
        with SafeZip(data) as container:
            names = set(container.names())
            if "[Content_Types].xml" in names:
                content_types = container.read("[Content_Types].xml").lower()
                if b"macroenabled" in content_types or b"vbaproject" in content_types:
                    raise DocumentIdentityError("active_content_detected")
                if "word/document.xml" in names:
                    label = "docx"
                elif "xl/workbook.xml" in names:
                    label = "xlsx"
                elif "ppt/presentation.xml" in names:
                    label = "pptx"
                else:
                    raise DocumentIdentityError("unsupported_format")
                _xml_package_policy(container, label)
                return label
            mimetype = container.read("mimetype", limit=256) if "mimetype" in names else b""
            if mimetype == b"application/epub+zip":
                if "META-INF/container.xml" not in names:
                    raise DocumentIdentityError("corrupted_document")
                _epub_policy(container)
                return "epub"
            odf = {
                b"application/vnd.oasis.opendocument.text": "odt",
                b"application/vnd.oasis.opendocument.spreadsheet": "ods",
                b"application/vnd.oasis.opendocument.presentation": "odp",
            }.get(mimetype)
            if odf:
                if "META-INF/manifest.xml" not in names or "content.xml" not in names:
                    raise DocumentIdentityError("corrupted_document")
                _odf_policy(container, odf)
                return odf
            raise DocumentIdentityError("unsupported_format")
    except DocumentIdentityError:
        raise
    except ContainerError as exc:
        _raise_container(exc)


def _text_identity(data: bytes, extension: str) -> tuple[str, list[str]]:
    text, warnings = _decode_text(data)
    stripped = text.lstrip()
    lower = stripped[:8_192].casefold()
    if extension in {".html", ".htm", ".xhtml"} or re.match(
        r"(?:<!doctype\s+html|<html\b|<head\b|<body\b|<main\b|<article\b)",
        lower,
    ):
        _inspect_html_policy(text)
        return "html", warnings
    sample = text[:128_000]
    delimiter = None
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
        rows = list(csv.reader(io.StringIO(sample), dialect))[:50]
        widths = [len(row) for row in rows if row]
        if not widths or max(widths) < 2 or sum(width == widths[0] for width in widths) < max(2, len(widths) // 2):
            delimiter = None
    except (csv.Error, UnicodeError):
        delimiter = None
    if extension == ".tsv" or delimiter == "\t":
        return "tsv", warnings
    if extension == ".csv" or delimiter in {",", ";", "|"}:
        return "csv", warnings
    if extension in {".md", ".markdown"} or re.search(
        r"(?m)^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|```|~~~)", text[:64_000],
    ):
        if re.search(r"<\s*[A-Za-z][^>]*>", text):
            _inspect_html_policy(text)
        return "markdown", warnings
    return "txt", warnings


def _mismatch_warnings(format_label: str, extension: str, declared_mime: str) -> tuple[str, ...]:
    warnings: list[str] = []
    expected = _KNOWN_EXTENSIONS.get(extension)
    if expected and expected not in {format_label, "xls", "ppt"}:
        warnings.append("file_identity_mismatch")
    elif expected in {"xls", "ppt"} and format_label not in {expected}:
        warnings.append("file_identity_mismatch")
    mime = str(declared_mime or "").split(";", 1)[0].strip().casefold()
    accepted = _FORMAT_MIMES.get(format_label, set())
    if mime and mime not in accepted and mime not in {"application/octet-stream", "application/zip"}:
        if "file_identity_mismatch" not in warnings:
            warnings.append("file_identity_mismatch")
    return tuple(warnings)


def identify_document(
    data: bytes,
    *,
    display_name: str = "",
    declared_mime: str = "",
) -> DocumentIdentity:
    """Identify one file by signature/content and enforce passive policy."""

    value = bytes(data)
    extension = Path(str(display_name or "")).suffix.casefold()
    if not value:
        raise DocumentIdentityError("corrupted_document")
    if value.startswith(_EXECUTABLE_PREFIXES) or value.startswith(b"#!"):
        raise DocumentIdentityError("active_content_detected")
    if value.startswith(_CFB):
        folded = value.lower()
        if (
            "EncryptedPackage".encode("utf-16le").lower() in folded
            or "EncryptionInfo".encode("utf-16le").lower() in folded
        ):
            raise DocumentIdentityError("encrypted_document")
        raise DocumentIdentityError("unsupported_legacy_format")
    if value.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        label = _zip_identity(value)
        return DocumentIdentity(label, _FAMILIES[label], _mismatch_warnings(label, extension, declared_mime))
    if value.startswith(b"%PDF-"):
        folded = value.lower()
        if re.search(rb"/encrypt\s+\d+\s+\d+\s+r\b", folded):
            raise DocumentIdentityError("encrypted_document")
        if any(token in folded for token in (b"/javascript", b"/launch", b"/embeddedfile")):
            raise DocumentIdentityError("active_content_detected")
        if re.search(rb"/js\s*(?:\(|<|\d+\s+\d+\s+r)", folded):
            raise DocumentIdentityError("active_content_detected")
        if b"/gotor" in folded or b"/submitform" in folded or b"/importdata" in folded:
            raise DocumentIdentityError("external_reference_detected")
        return DocumentIdentity("pdf", "document", _mismatch_warnings("pdf", extension, declared_mime))
    if value.startswith(_PNG):
        return DocumentIdentity("png", "image", _mismatch_warnings("png", extension, declared_mime))
    if value.startswith(_JPEG):
        return DocumentIdentity("jpeg", "image", _mismatch_warnings("jpeg", extension, declared_mime))
    if _RTF_RE.match(value[:64]):
        _inspect_rtf(value)
        return DocumentIdentity("rtf", "document", _mismatch_warnings("rtf", extension, declared_mime))
    label, text_warnings = _text_identity(value, extension)
    expected = _KNOWN_EXTENSIONS.get(extension)
    if expected in {"docx", "odt", "epub", "xlsx", "ods", "pptx", "odp", "pdf", "png", "jpeg"}:
        raise DocumentIdentityError("file_identity_mismatch")
    warnings = list(text_warnings) + list(_mismatch_warnings(label, extension, declared_mime))
    return DocumentIdentity(label, _FAMILIES[label], tuple(dict.fromkeys(warnings)))


__all__ = [
    "DocumentIdentity", "DocumentIdentityError", "SUPPORTED_FORMATS",
    "identify_document",
]
