"""Bounded, immutable structured-document representation for WOR-187.

This module deliberately contains no filesystem or network access.  Extractors
build one JSON-serializable representation here, then the private artifact store
commits it atomically.  Stable ordinal identifiers make query results
deterministic without exposing source names, paths, or integrity digests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 2
MAX_OUTPUT_CHARS = 2_000_000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = 2_048
CHUNK_CHARS = 4_000
MAX_PARAGRAPHS = 200_000
MAX_TABLES = 5_000
MAX_TABLE_CELLS = 1_000_000
MAX_SHEETS = 256
MAX_POPULATED_CELLS = 1_000_000
MAX_SLIDES = 1_000
MAX_SHAPES_PER_SLIDE = 2_000
MAX_SHAPES_TOTAL = 50_000
MAX_EMBEDDED = 1_000
MAX_EMBEDDED_BYTES = 64 * 1024 * 1024

SUPPORTED_QUERY_OPERATIONS = (
    "summary", "search", "chunks", "sections", "headings", "table",
    "sheets", "range", "formula", "slides", "notes", "embedded",
    "provenance", "metadata", "ocr", "frames", "pages", "transcript",
    "time_range", "timeline", "tracks", "chapters",
)

_SAFE_WARNING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SPACE_RE = re.compile(r"[\t\x0b\x0c\r ]+")


class StructuredDocumentError(RuntimeError):
    """One stable structured-extraction failure with no private details."""

    def __init__(self, category: str, *, limit_kind: str | None = None):
        super().__init__(category)
        self.category = category
        self.limit_kind = limit_kind


def clean_text(value: Any) -> str:
    """Normalize passive extracted text without interpreting it."""

    text = str(value or "").replace("\x00", "")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def safe_warning(value: Any) -> str | None:
    code = str(value or "")
    return code if _SAFE_WARNING_RE.fullmatch(code) else None


def _cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        for key in ("display_value", "cached_value", "value", "text", "formula"):
            value = cell.get(key)
            if value is not None and str(value):
                return clean_text(value)
        return ""
    return clean_text(cell)


@dataclass
class DocumentBuilder:
    """Mutable bounded builder; :meth:`finish` returns the immutable payload."""

    format_label: str
    family: str
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    paragraphs: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    headings: list[dict] = field(default_factory=list)
    lists: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    sheets: list[dict] = field(default_factory=list)
    slides: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    embedded: list[dict] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)
    _records: list[tuple[str, dict]] = field(default_factory=list)
    _table_cells: int = 0
    _sheet_cells: int = 0
    _shape_count: int = 0
    _embedded_bytes: int = 0

    def warn(self, code: str) -> None:
        value = safe_warning(code)
        if value and value not in self.warnings and len(self.warnings) < 32:
            self.warnings.append(value)

    def _id(self, prefix: str, ordinal: int) -> str:
        return f"{prefix}_{ordinal:06d}"

    def _record(self, text: str, provenance: dict | None = None) -> None:
        value = clean_text(text)
        if value:
            self._records.append((value, dict(provenance or {})))

    def add_paragraph(
        self,
        text: str,
        *,
        kind: str = "paragraph",
        level: int | None = None,
        style: str | None = None,
        provenance: dict | None = None,
        links: list[dict] | None = None,
    ) -> str | None:
        value = clean_text(text)
        if not value:
            return None
        if len(self.paragraphs) >= MAX_PARAGRAPHS:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="paragraph_count",
            )
        paragraph_id = self._id("paragraph", len(self.paragraphs) + 1)
        item = {
            "paragraph_id": paragraph_id,
            "ordinal": len(self.paragraphs),
            "kind": str(kind or "paragraph")[:32],
            "text": value,
            "provenance": dict(provenance or {}),
        }
        if level is not None:
            item["level"] = max(1, min(int(level), 64))
        if style:
            item["style"] = clean_text(style)[:128]
        self.paragraphs.append(item)
        self._record(value, item["provenance"])
        if kind == "heading":
            heading = {
                "heading_id": self._id("heading", len(self.headings) + 1),
                "paragraph_id": paragraph_id,
                "ordinal": len(self.headings),
                "level": item.get("level", 1),
                "text": value,
                "provenance": dict(item["provenance"]),
            }
            self.headings.append(heading)
            self.sections.append({
                "section_id": self._id("section", len(self.sections) + 1),
                "ordinal": len(self.sections),
                "heading_id": heading["heading_id"],
                "title": value,
                "level": heading["level"],
                "provenance": dict(item["provenance"]),
            })
        if links:
            for link in links[:128]:
                self.add_link(
                    link.get("text", ""), link.get("target", ""),
                    provenance=item["provenance"],
                )
        return paragraph_id

    def add_list_item(
        self,
        text: str,
        *,
        ordered: bool = False,
        level: int = 1,
        provenance: dict | None = None,
    ) -> str | None:
        paragraph_id = self.add_paragraph(
            text, kind="list_item", level=level, provenance=provenance,
        )
        if paragraph_id:
            self.lists.append({
                "list_item_id": self._id("list_item", len(self.lists) + 1),
                "paragraph_id": paragraph_id,
                "ordinal": len(self.lists),
                "ordered": bool(ordered),
                "level": max(1, min(int(level), 64)),
            })
        return paragraph_id

    def add_link(
        self, text: str, target: str, *, provenance: dict | None = None,
    ) -> None:
        if len(self.links) >= 50_000:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="relationship_edges",
            )
        passive_target = clean_text(target)[:2_048]
        if not passive_target:
            return
        self.links.append({
            "link_id": self._id("link", len(self.links) + 1),
            "ordinal": len(self.links),
            "text": clean_text(text)[:1_000],
            "target": passive_target,
            "inert": True,
            "provenance": dict(provenance or {}),
        })

    def add_table(
        self,
        rows: list[list[Any]],
        *,
        name: str | None = None,
        provenance: dict | None = None,
    ) -> str:
        if len(self.tables) >= MAX_TABLES:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="table_count",
            )
        normalized: list[list[dict]] = []
        for row_index, row in enumerate(rows):
            out_row = []
            for column_index, raw in enumerate(row):
                self._table_cells += 1
                if self._table_cells > MAX_TABLE_CELLS:
                    raise StructuredDocumentError(
                        "resource_limit_exceeded", limit_kind="table_cells",
                    )
                cell = dict(raw) if isinstance(raw, dict) else {"text": clean_text(raw)}
                cell.setdefault("row", row_index + 1)
                cell.setdefault("column", column_index + 1)
                for key in ("text", "value", "display_value", "cached_value", "formula"):
                    if key in cell and cell[key] is not None:
                        cell[key] = clean_text(cell[key])
                out_row.append(cell)
            normalized.append(out_row)
        table_id = self._id("table", len(self.tables) + 1)
        item = {
            "table_id": table_id,
            "ordinal": len(self.tables),
            "name": clean_text(name)[:256] if name else "",
            "rows": normalized,
            "row_count": len(normalized),
            "column_count": max((len(row) for row in normalized), default=0),
            "provenance": dict(provenance or {}),
        }
        self.tables.append(item)
        for row in normalized:
            self._record("\t".join(_cell_text(cell) for cell in row), item["provenance"])
        return table_id

    def add_sheet(self, sheet: dict) -> str:
        if len(self.sheets) >= MAX_SHEETS:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="sheet_count",
            )
        cells = list(sheet.get("cells") or [])
        self._sheet_cells += len(cells)
        if self._sheet_cells > MAX_POPULATED_CELLS:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="populated_cells",
            )
        sheet_id = self._id("sheet", len(self.sheets) + 1)
        item = {
            **dict(sheet),
            "sheet_id": sheet_id,
            "ordinal": len(self.sheets),
            "name": clean_text(sheet.get("name"))[:256],
            "state": str(sheet.get("state") or "visible")[:32],
            "cells": cells,
        }
        self.sheets.append(item)
        for cell in cells:
            self._record(
                f"{item['name']}!{cell.get('coordinate', '')}: {_cell_text(cell)}",
                {"sheet_id": sheet_id, "cell": cell.get("coordinate", "")},
            )
        return sheet_id

    def add_slide(self, slide: dict) -> str:
        if len(self.slides) >= MAX_SLIDES:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="slide_count",
            )
        shapes = list(slide.get("shapes") or [])
        if len(shapes) > MAX_SHAPES_PER_SLIDE:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="shapes_per_slide",
            )
        self._shape_count += len(shapes)
        if self._shape_count > MAX_SHAPES_TOTAL:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="shape_count",
            )
        slide_id = self._id("slide", len(self.slides) + 1)
        item = {
            **dict(slide),
            "slide_id": slide_id,
            "ordinal": len(self.slides),
            "shapes": shapes,
        }
        self.slides.append(item)
        for shape in shapes:
            self._record(
                shape.get("text", ""),
                {"slide_id": slide_id, "shape_id": shape.get("shape_id", "")},
            )
        notes_text = clean_text(item.get("notes"))
        if notes_text:
            note = {
                "note_id": self._id("note", len(self.notes) + 1),
                "ordinal": len(self.notes),
                "slide_id": slide_id,
                "text": notes_text,
            }
            self.notes.append(note)
            self._record(notes_text, {"slide_id": slide_id, "kind": "notes"})
        return slide_id

    def add_embedded(
        self,
        kind: str,
        *,
        media_type: str = "application/octet-stream",
        size: int = 0,
        provenance: dict | None = None,
    ) -> str:
        if len(self.embedded) >= MAX_EMBEDDED:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="embedded_count",
            )
        safe_size = max(0, int(size or 0))
        self._embedded_bytes += safe_size
        if self._embedded_bytes > MAX_EMBEDDED_BYTES:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="embedded_bytes",
            )
        embedded_id = self._id("embedded", len(self.embedded) + 1)
        self.embedded.append({
            "embedded_artifact_id": embedded_id,
            "ordinal": len(self.embedded),
            "kind": clean_text(kind)[:64] or "media",
            "media_type": clean_text(media_type)[:160],
            "size": safe_size,
            "opaque": True,
            "analysis_deferred": True,
            "provenance": dict(provenance or {}),
        })
        return embedded_id

    def _chunks(self) -> list[dict]:
        chunks: list[dict] = []
        buffer = ""
        origins: list[dict] = []
        for text, origin in self._records:
            remaining = text
            while remaining:
                separator = "\n" if buffer else ""
                capacity = CHUNK_CHARS - len(buffer) - len(separator)
                if capacity <= 0:
                    chunks.append(self._make_chunk(chunks, buffer, origins))
                    buffer, origins = "", []
                    continue
                piece = remaining[:capacity]
                buffer += separator + piece
                if origin and origin not in origins and len(origins) < 16:
                    origins.append(origin)
                remaining = remaining[len(piece):]
                if len(buffer) >= CHUNK_CHARS:
                    chunks.append(self._make_chunk(chunks, buffer, origins))
                    buffer, origins = "", []
                if len(chunks) >= MAX_CHUNKS and (remaining or buffer):
                    raise StructuredDocumentError(
                        "resource_limit_exceeded", limit_kind="chunk_count",
                    )
        if buffer:
            chunks.append(self._make_chunk(chunks, buffer, origins))
        return chunks

    @staticmethod
    def _make_chunk(chunks: list[dict], text: str, origins: list[dict]) -> dict:
        ordinal = len(chunks)
        return {
            "chunk_id": f"chunk_{ordinal + 1:06d}",
            "ordinal": ordinal,
            "text": text,
            "provenance": list(origins),
        }

    def finish(self) -> dict:
        chunks = self._chunks()
        document = {
            "schema_version": SCHEMA_VERSION,
            "format_label": str(self.format_label),
            "family": str(self.family),
            "status": "complete",
            "warnings": sorted(set(self.warnings)),
            "metadata": dict(self.metadata),
            "chunks": chunks,
            "sections": self.sections,
            "headings": self.headings,
            "paragraphs": self.paragraphs,
            "lists": self.lists,
            "tables": self.tables,
            "sheets": self.sheets,
            "slides": self.slides,
            "notes": self.notes,
            "links": self.links,
            "embedded": self.embedded,
            "provenance": self.provenance,
        }
        text_chars = sum(len(chunk["text"]) for chunk in chunks)
        if text_chars > MAX_OUTPUT_CHARS:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="output_size",
            )
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise StructuredDocumentError(
                "resource_limit_exceeded", limit_kind="output_size",
            )
        return document


def document_counts(document: dict) -> dict:
    """Return the only counts that may appear in public references."""

    pages = int((document.get("metadata") or {}).get("page_count", 0) or 0)
    return {
        "page_count": max(0, pages),
        "frame_count": len(document.get("frames") or []),
        "track_count": len(document.get("tracks") or []),
        "segment_count": len(document.get("transcript") or []),
        "chapter_count": len(document.get("chapters") or []),
        "section_count": len(document.get("sections") or []),
        "table_count": len(document.get("tables") or []),
        "sheet_count": len(document.get("sheets") or []),
        "slide_count": len(document.get("slides") or []),
        "chunk_count": len(document.get("chunks") or []),
        "embedded_count": len(document.get("embedded") or []),
    }


def baseline_document(content: str, metadata: dict) -> dict:
    """Adapt the proven WOR-175 PDF/image result to artifact schema v2."""

    document_type = str(metadata.get("document_type") or "pdf")
    builder = DocumentBuilder(document_type, "image" if document_type != "pdf" else "document")
    builder.metadata.update({
        "page_count": int(metadata.get("page_count", 0) or 0),
        "page_methods": list(metadata.get("page_methods") or []),
        "confidence": metadata.get("confidence"),
    })
    for warning in metadata.get("warnings") or []:
        builder.warn(str(warning))
    page_parts = re.split(r"(?m)^--- Page (\d+) ---\n", str(content or ""))
    if len(page_parts) > 1:
        for index in range(1, len(page_parts), 2):
            page_number = int(page_parts[index])
            builder.add_paragraph(
                page_parts[index + 1],
                provenance={"page": page_number},
            )
    else:
        builder.add_paragraph(content, provenance={"page": 1})
    return builder.finish()


__all__ = [
    "CHUNK_CHARS", "DocumentBuilder", "MAX_OUTPUT_BYTES", "MAX_OUTPUT_CHARS",
    "SCHEMA_VERSION", "SUPPORTED_QUERY_OPERATIONS", "StructuredDocumentError",
    "baseline_document", "clean_text", "document_counts", "safe_warning",
]
