"""Standard-library structured extractors for passive document formats."""

from __future__ import annotations

import csv
import html
import io
import posixpath
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote

from osia.document_containers import (
    ContainerError,
    SafeZip,
    inspect_relationships,
    local_name,
    resolve_relationship_target,
)
from osia.document_models import (
    DocumentBuilder,
    MAX_POPULATED_CELLS,
    StructuredDocumentError,
    clean_text,
)


MAX_ROW = 1_048_576
MAX_COLUMN = 16_384
_A1_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
_REMOTE_RE = re.compile(r"^(?:https?|ftp|ftps|file|smb|data|javascript):|^//", re.I)


def _attrs(element) -> dict[str, str]:
    return {local_name(key): str(value) for key, value in element.attrib.items()}


def _text(element) -> str:
    return clean_text("".join(element.itertext()))


def _children(element, name: str):
    return [child for child in list(element) if local_name(child.tag) == name]


def _descendants(element, name: str):
    return [child for child in element.iter() if local_name(child.tag) == name]


def _rels_by_source(container: SafeZip) -> dict[str, dict[str, dict]]:
    result: dict[str, dict[str, dict]] = {}
    for relationship in inspect_relationships(container):
        result.setdefault(relationship["source"], {})[relationship["id"]] = relationship
    return result


def _media_type(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
        "emf": "image/emf", "wmf": "image/wmf", "mp3": "audio/mpeg",
        "wav": "audio/wav", "mp4": "video/mp4",
    }.get(suffix, "application/octet-stream")


def _record_media(builder: DocumentBuilder, container: SafeZip, prefixes: tuple[str, ...]) -> None:
    for member in container.members:
        if member.name.startswith(prefixes):
            builder.add_embedded(
                "image" if _media_type(member.name).startswith("image/") else "media",
                media_type=_media_type(member.name),
                size=member.size,
                provenance={"package_part": member.name},
            )


def _word_paragraph(
    builder: DocumentBuilder,
    paragraph,
    provenance: dict,
    relationships: dict[str, dict],
    numbering: dict[tuple[str, int], bool] | None = None,
) -> None:
    value = _text(paragraph)
    if not value:
        return
    style = ""
    ordered = False
    is_list = False
    list_level = 1
    num_id = ""
    for child in paragraph.iter():
        tag = local_name(child.tag)
        attrs = _attrs(child)
        if tag == "pStyle":
            style = attrs.get("val", "")
        elif tag == "numPr":
            is_list = True
        elif tag == "ilvl":
            try:
                list_level = int(attrs.get("val", "0")) + 1
            except ValueError:
                list_level = 1
        elif tag == "numId":
            num_id = attrs.get("val", "")
    if is_list:
        ordered = (numbering or {}).get((num_id, list_level - 1), True)
    heading_match = re.search(r"heading\s*([0-9]+)", style, re.I)
    links = []
    for link in _descendants(paragraph, "hyperlink"):
        attrs = _attrs(link)
        rel = relationships.get(attrs.get("id", ""))
        target = rel.get("target", "") if rel else attrs.get("anchor", "")
        if target:
            links.append({"text": _text(link), "target": target})
    if heading_match:
        builder.add_paragraph(
            value, kind="heading", level=int(heading_match.group(1)),
            style=style, provenance=provenance, links=links,
        )
    elif is_list:
        builder.add_list_item(
            value, ordered=ordered, level=list_level, provenance=provenance,
        )
        for link in links:
            builder.add_link(link["text"], link["target"], provenance=provenance)
    else:
        builder.add_paragraph(value, style=style, provenance=provenance, links=links)


def _word_numbering(container: SafeZip) -> dict[tuple[str, int], bool]:
    if not container.has("word/numbering.xml"):
        return {}
    root = container.xml("word/numbering.xml")
    abstract: dict[tuple[str, int], bool] = {}
    num_to_abstract: dict[str, str] = {}
    for element in root.iter():
        tag = local_name(element.tag)
        attrs = _attrs(element)
        if tag == "abstractNum":
            abstract_id = attrs.get("abstractNumId", "")
            for level in _children(element, "lvl"):
                level_attrs = _attrs(level)
                try:
                    index = int(level_attrs.get("ilvl", "0"))
                except ValueError:
                    index = 0
                fmt = next((_attrs(item).get("val", "") for item in level.iter() if local_name(item.tag) == "numFmt"), "")
                abstract[(abstract_id, index)] = fmt.casefold() != "bullet"
        elif tag == "num":
            num_id = attrs.get("numId", "")
            abstract_id = next((_attrs(item).get("val", "") for item in element.iter() if local_name(item.tag) == "abstractNumId"), "")
            if num_id and abstract_id:
                num_to_abstract[num_id] = abstract_id
    return {
        (num_id, level): ordered
        for num_id, abstract_id in num_to_abstract.items()
        for (candidate, level), ordered in abstract.items()
        if candidate == abstract_id
    }


def _xml_table_rows(table) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for row in _descendants(table, "tr"):
        values = []
        for cell in list(row):
            if local_name(cell.tag) in {"tc", "td", "th", "table-cell", "covered-table-cell"}:
                values.append({"text": _text(cell)})
        if values:
            rows.append(values)
    if rows:
        return rows
    for row in _descendants(table, "table-row"):
        values = []
        for cell in list(row):
            if local_name(cell.tag) in {"table-cell", "covered-table-cell"}:
                values.append({"text": _text(cell)})
        if values:
            rows.append(values)
    return rows


def _extract_docx(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("docx", "document")
        for warning in warnings:
            builder.warn(warning)
        relationships = _rels_by_source(container)
        numbering = _word_numbering(container)
        document = container.xml("word/document.xml")
        doc_rels = relationships.get("word/document.xml", {})
        paragraph_number = 0
        table_number = 0
        body = next((item for item in document.iter() if local_name(item.tag) == "body"), document)
        for child in list(body):
            tag = local_name(child.tag)
            if tag == "p":
                paragraph_number += 1
                _word_paragraph(
                    builder, child, {"part": "body", "paragraph": paragraph_number}, doc_rels,
                    numbering,
                )
            elif tag == "tbl":
                table_number += 1
                builder.add_table(
                    _xml_table_rows(child),
                    provenance={"part": "body", "table": table_number},
                )
            elif tag == "sectPr":
                builder.metadata["section_properties_count"] = int(
                    builder.metadata.get("section_properties_count", 0)
                ) + 1
        supplemental = (
            ("word/footnotes.xml", "footnote"),
            ("word/endnotes.xml", "endnote"),
            ("word/comments.xml", "comment"),
        )
        for part, kind in supplemental:
            if not container.has(part):
                continue
            root = container.xml(part)
            for item in root.iter():
                if local_name(item.tag) != kind:
                    continue
                value = _text(item)
                if value:
                    builder.add_paragraph(
                        value, kind=kind, provenance={"part": kind, "id": _attrs(item).get("id", "")},
                    )
        for name in container.names():
            if not re.fullmatch(r"word/(?:header|footer)\d+\.xml", name):
                continue
            kind = "header" if "/header" in name else "footer"
            root = container.xml(name)
            for paragraph in _descendants(root, "p"):
                _word_paragraph(
                    builder, paragraph, {"part": kind},
                    relationships.get(name, {}), numbering,
                )
        bookmarks = []
        for element in document.iter():
            if local_name(element.tag) == "bookmarkStart":
                name = clean_text(_attrs(element).get("name", ""))[:128]
                if name and not name.startswith("_"):
                    bookmarks.append(name)
        builder.metadata["bookmarks"] = bookmarks[:10_000]
        _record_media(builder, container, ("word/media/",))
        return builder.finish()


def _walk_odf_document(builder: DocumentBuilder, root, *, part: str = "body") -> None:
    table_number = len(builder.tables)

    def visit(element, list_level: int = 0):
        nonlocal table_number
        tag = local_name(element.tag)
        attrs = _attrs(element)
        provenance = {"part": part}
        if tag == "h":
            try:
                level = int(attrs.get("outline-level", "1"))
            except ValueError:
                level = 1
            builder.add_paragraph(_text(element), kind="heading", level=level, provenance=provenance)
            return
        if tag == "p":
            builder.add_paragraph(_text(element), provenance=provenance)
            for link in _descendants(element, "a"):
                target = _attrs(link).get("href", "")
                if target:
                    builder.add_link(_text(link), target, provenance=provenance)
            return
        if tag == "list-item":
            own = " ".join(_text(child) for child in list(element) if local_name(child.tag) == "p")
            builder.add_list_item(own, level=max(1, list_level), provenance=provenance)
            for child in list(element):
                if local_name(child.tag) not in {"p"}:
                    visit(child, list_level + 1)
            return
        if tag == "table":
            table_number += 1
            builder.add_table(
                _xml_table_rows(element), name=attrs.get("name", ""),
                provenance={"part": part, "table": table_number},
            )
            return
        if tag == "annotation":
            builder.add_paragraph(_text(element), kind="comment", provenance=provenance)
            return
        next_level = list_level + 1 if tag == "list" else list_level
        for child in list(element):
            visit(child, next_level)

    visit(root)


def _extract_odt(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("odt", "document")
        for warning in warnings:
            builder.warn(warning)
        _walk_odf_document(builder, container.xml("content.xml"))
        if container.has("styles.xml"):
            styles = container.xml("styles.xml")
            for element in styles.iter():
                tag = local_name(element.tag)
                if tag in {"header", "footer"}:
                    value = _text(element)
                    if value:
                        builder.add_paragraph(value, kind=tag, provenance={"part": "styles"})
        _record_media(builder, container, ("Pictures/",))
        return builder.finish()


class _PassiveHTMLParser(HTMLParser):
    _BLOCKS = frozenset({"p", "div", "main", "article", "section", "blockquote", "pre", "figcaption"})

    def __init__(self, builder: DocumentBuilder, *, provenance: dict | None = None):
        super().__init__(convert_charrefs=True)
        self.builder = builder
        self.provenance = dict(provenance or {})
        self.stack: list[str] = []
        self.buffer: list[str] = []
        self.current_kind = "paragraph"
        self.current_level = 1
        self.current_link: tuple[str, int] | None = None
        self.table: list[list[dict]] | None = None
        self.row: list[dict] | None = None
        self.cell: list[str] | None = None

    def _flush(self) -> None:
        value = clean_text("".join(self.buffer))
        self.buffer = []
        if not value:
            return
        if self.current_kind == "heading":
            self.builder.add_paragraph(
                value, kind="heading", level=self.current_level,
                provenance=self.provenance,
            )
        elif self.current_kind == "list_item":
            self.builder.add_list_item(value, provenance=self.provenance)
        else:
            self.builder.add_paragraph(value, provenance=self.provenance)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        mapping = {key.casefold(): str(value or "") for key, value in attrs}
        self.stack.append(tag)
        if tag in self._BLOCKS or tag in {"li"} or re.fullmatch(r"h[1-6]", tag):
            self._flush()
            if tag == "li":
                self.current_kind = "list_item"
            elif tag.startswith("h"):
                self.current_kind = "heading"
                self.current_level = int(tag[1])
            else:
                self.current_kind = "paragraph"
        if tag == "a":
            self.current_link = (mapping.get("href", ""), len(self.buffer))
        elif tag == "table":
            self._flush()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self.current_link:
            target, start = self.current_link
            label = clean_text("".join(self.buffer[start:]))
            if target:
                self.builder.add_link(label, target, provenance=self.provenance)
            self.current_link = None
        elif tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append({"text": clean_text("".join(self.cell))})
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.builder.add_table(self.table, provenance=self.provenance)
            self.table = None
        elif tag in self._BLOCKS or tag == "li" or re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.current_kind = "paragraph"
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.buffer.append(data)

    def finish(self) -> None:
        self._flush()


def _parse_html_into(builder: DocumentBuilder, text: str, provenance: dict | None = None) -> None:
    parser = _PassiveHTMLParser(builder, provenance=provenance)
    try:
        parser.feed(text)
        parser.close()
        parser.finish()
    except (ValueError, RecursionError) as exc:
        raise StructuredDocumentError("corrupted_document") from exc


def _decode_text(data: bytes) -> tuple[str, str, bool]:
    if data.startswith(b"\xef\xbb\xbf"):
        codec = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        codec = "utf-16"
    else:
        codec = "utf-8"
    try:
        return data.decode(codec), codec, False
    except UnicodeDecodeError:
        return data.decode(codec, errors="replace"), codec, True


def _extract_text(data: bytes, format_label: str, warnings: tuple[str, ...]) -> dict:
    text, encoding, replaced = _decode_text(data)
    builder = DocumentBuilder(format_label, "document")
    builder.metadata["encoding"] = encoding
    for warning in warnings:
        builder.warn(warning)
    if replaced:
        builder.warn("decoding_replacements")
    if format_label == "html":
        _parse_html_into(builder, text, {"part": "document"})
    elif format_label == "markdown":
        lines = text.splitlines()
        paragraph: list[str] = []
        table_lines: list[str] = []
        in_code = False

        def flush_paragraph():
            if paragraph:
                builder.add_paragraph("\n".join(paragraph), provenance={"part": "document"})
                paragraph.clear()

        def flush_table():
            if table_lines:
                rows = []
                for line in table_lines:
                    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                    if cells and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                        rows.append([{"text": cell} for cell in cells])
                if rows:
                    builder.add_table(rows, provenance={"part": "document"})
                table_lines.clear()

        for line in lines:
            if re.match(r"^\s*(```|~~~)", line):
                flush_paragraph(); flush_table()
                in_code = not in_code
                builder.add_paragraph(line, kind="code", provenance={"part": "document"})
                continue
            if in_code:
                builder.add_paragraph(line, kind="code", provenance={"part": "document"})
                continue
            heading = re.match(r"^(#{1,6})\s+(.*)$", line)
            item = re.match(r"^\s*(?:(\d+)[.)]|[-*+])\s+(.*)$", line)
            if heading:
                flush_paragraph(); flush_table()
                builder.add_paragraph(
                    heading.group(2), kind="heading", level=len(heading.group(1)),
                    provenance={"part": "document"},
                )
            elif item:
                flush_paragraph(); flush_table()
                builder.add_list_item(
                    item.group(2), ordered=bool(item.group(1)), provenance={"part": "document"},
                )
            elif "|" in line and line.strip():
                flush_paragraph()
                table_lines.append(line)
            elif not line.strip():
                flush_paragraph(); flush_table()
            elif line.lstrip().startswith("<"):
                flush_paragraph(); flush_table()
                _parse_html_into(builder, line, {"part": "inline_html"})
            else:
                paragraph.append(line)
                for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", line):
                    builder.add_link(match.group(1), match.group(2), provenance={"part": "document"})
        flush_paragraph(); flush_table()
    else:
        for paragraph in re.split(r"(?:\r?\n){2,}", text):
            builder.add_paragraph(paragraph, provenance={"part": "document"})
    return builder.finish()


def _extract_rtf(data: bytes, warnings: tuple[str, ...]) -> dict:
    builder = DocumentBuilder("rtf", "document")
    for warning in warnings:
        builder.warn(warning)
    raw = data.decode("latin-1")
    raw = re.sub(r"\\'(\w{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), raw)
    raw = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), raw)
    raw = raw.replace("\\par", "\n\n").replace("\\line", "\n").replace("\\tab", "\t")
    raw = re.sub(r"\\[A-Za-z]+-?\d* ?", "", raw)
    raw = re.sub(r"[{}]", "", raw)
    raw = raw.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    for paragraph in re.split(r"\n\s*\n", raw):
        value = clean_text(paragraph)
        if value:
            builder.add_paragraph(value, provenance={"part": "document"})
    builder.warn("rtf_structure_limited")
    return builder.finish()


def _extract_epub(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("epub", "document")
        for warning in warnings:
            builder.warn(warning)
        container_root = container.xml("META-INF/container.xml")
        rootfile = next((item for item in container_root.iter() if local_name(item.tag) == "rootfile"), None)
        if rootfile is None:
            raise StructuredDocumentError("corrupted_document")
        opf_name = _attrs(rootfile).get("full-path", "")
        if not opf_name or not container.has(opf_name):
            raise StructuredDocumentError("corrupted_document")
        opf = container.xml(opf_name)
        manifest = {}
        spine = []
        for element in opf.iter():
            attrs = _attrs(element)
            if local_name(element.tag) == "item":
                manifest[attrs.get("id", "")] = attrs
            elif local_name(element.tag) == "itemref":
                spine.append(attrs.get("idref", ""))
            elif local_name(element.tag) in {"title", "creator", "language"}:
                value = _text(element)
                if value:
                    builder.metadata[local_name(element.tag)] = value[:1_000]
        base = posixpath.dirname(opf_name)
        for chapter_index, item_id in enumerate(spine, start=1):
            item = manifest.get(item_id)
            if not item:
                raise StructuredDocumentError("corrupted_document")
            name = resolve_relationship_target(opf_name, unquote(item.get("href", "")))
            if not container.has(name):
                raise StructuredDocumentError("corrupted_document")
            text, _codec, _replaced = _decode_text(container.read(name))
            _parse_html_into(
                builder, text,
                {"chapter": chapter_index, "spine_id": item_id},
            )
        for item in manifest.values():
            media = item.get("media-type", "")
            if media.startswith(("image/", "audio/", "video/")):
                name = posixpath.normpath(posixpath.join(base, unquote(item.get("href", ""))))
                if container.has(name):
                    builder.add_embedded(
                        "image" if media.startswith("image/") else "media",
                        media_type=media, size=container.info(name).size,
                        provenance={"manifest_id": item.get("id", "")},
                    )
        builder.metadata["spine_count"] = len(spine)
        return builder.finish()


def _column_number(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + ord(char) - 64
    return value


def _coordinate(value: str) -> tuple[str, int, int]:
    match = _A1_RE.fullmatch(str(value or ""))
    if not match:
        raise StructuredDocumentError("corrupted_document")
    column = _column_number(match.group(1))
    row = int(match.group(2))
    if row > MAX_ROW or column > MAX_COLUMN:
        raise StructuredDocumentError("resource_limit_exceeded", limit_kind="worksheet_coordinate")
    return f"{match.group(1).upper()}{row}", row, column


def _shared_strings(container: SafeZip) -> list[str]:
    if not container.has("xl/sharedStrings.xml"):
        return []
    result = []
    root = container.xml("xl/sharedStrings.xml")
    for item in _descendants(root, "si"):
        result.append(clean_text("".join(node.text or "" for node in item.iter() if local_name(node.tag) == "t")))
    return result


def _xlsx_cell(element, shared: list[str], date_styles: set[int]) -> dict:
    attrs = _attrs(element)
    coordinate, row, column = _coordinate(attrs.get("r", ""))
    kind = attrs.get("t", "n")
    style_index = int(attrs.get("s", "0") or 0)
    formula_node = next((item for item in element if local_name(item.tag) == "f"), None)
    value_node = next((item for item in element if local_name(item.tag) == "v"), None)
    inline = next((item for item in element if local_name(item.tag) == "is"), None)
    raw_value = value_node.text if value_node is not None else ""
    cell: dict[str, Any] = {
        "coordinate": coordinate, "row": row, "column": column,
        "cell_type": kind, "style_index": style_index,
    }
    if formula_node is not None:
        cell["formula"] = clean_text(formula_node.text)
        cell["cached_value"] = clean_text(raw_value)
        cell["formula_inert"] = True
    if kind == "s":
        try:
            index = int(raw_value)
            if index < 0 or index >= len(shared):
                raise ValueError
            cell["value"] = shared[index]
        except ValueError as exc:
            raise StructuredDocumentError("corrupted_document") from exc
    elif kind == "inlineStr" and inline is not None:
        cell["value"] = _text(inline)
    elif kind == "b":
        cell["value"] = raw_value == "1"
    elif kind in {"str", "e", "d"}:
        cell["value"] = clean_text(raw_value)
    elif raw_value != "":
        try:
            number = float(raw_value)
            cell["value"] = int(number) if number.is_integer() else number
            if style_index in date_styles:
                cell["date_serial"] = number
                cell["cell_type"] = "date"
        except ValueError:
            cell["value"] = clean_text(raw_value)
    return cell


def _xlsx_date_styles(container: SafeZip) -> set[int]:
    if not container.has("xl/styles.xml"):
        return set()
    root = container.xml("xl/styles.xml")
    custom = {}
    for item in _descendants(root, "numFmt"):
        attrs = _attrs(item)
        try:
            custom[int(attrs.get("numFmtId", "0"))] = attrs.get("formatCode", "")
        except ValueError:
            continue
    styles = set()
    xfs = next((item for item in root.iter() if local_name(item.tag) == "cellXfs"), None)
    if xfs is not None:
        for index, xf in enumerate(list(xfs)):
            try:
                num_id = int(_attrs(xf).get("numFmtId", "0"))
            except ValueError:
                continue
            code = custom.get(num_id, "")
            if num_id in set(range(14, 23)) | {45, 46, 47} or re.search(r"[ymdhis]", code, re.I):
                styles.add(index)
    return styles


def _extract_xlsx(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("xlsx", "spreadsheet")
        for warning in warnings:
            builder.warn(warning)
        relationships = _rels_by_source(container)
        workbook_rels = relationships.get("xl/workbook.xml", {})
        workbook = container.xml("xl/workbook.xml")
        shared = _shared_strings(container)
        date_styles = _xlsx_date_styles(container)
        names = []
        for defined in _descendants(workbook, "definedName"):
            names.append({
                "name": _attrs(defined).get("name", "")[:256],
                "reference": _text(defined)[:4_096],
            })
        builder.metadata["named_ranges"] = names[:50_000]
        for sheet in _descendants(workbook, "sheet"):
            attrs = _attrs(sheet)
            rel = workbook_rels.get(attrs.get("id", ""))
            if not rel or not container.has(rel["target"]):
                raise StructuredDocumentError("corrupted_document")
            root = container.xml(rel["target"])
            cells = []
            hidden_rows = []
            hidden_columns = []
            merged = []
            hyperlinks = []
            sheet_rels = relationships.get(rel["target"], {})
            for row in _descendants(root, "row"):
                row_attrs = _attrs(row)
                if row_attrs.get("hidden") in {"1", "true"}:
                    hidden_rows.append(int(row_attrs.get("r", "0") or 0))
            for columns in _descendants(root, "col"):
                col_attrs = _attrs(columns)
                if col_attrs.get("hidden") in {"1", "true"}:
                    hidden_columns.append({
                        "min": int(col_attrs.get("min", "0") or 0),
                        "max": int(col_attrs.get("max", "0") or 0),
                    })
            for cell in _descendants(root, "c"):
                parsed_cell = _xlsx_cell(cell, shared, date_styles)
                if any(
                    parsed_cell.get(key) not in {None, ""}
                    for key in ("value", "formula", "cached_value")
                ):
                    cells.append(parsed_cell)
            for merge in _descendants(root, "mergeCell"):
                merged.append(_attrs(merge).get("ref", "")[:64])
            for link in _descendants(root, "hyperlink"):
                link_attrs = _attrs(link)
                relationship = sheet_rels.get(link_attrs.get("id", ""))
                target = relationship.get("target", "") if relationship else link_attrs.get("location", "")
                hyperlinks.append({"ref": link_attrs.get("ref", ""), "target": target, "inert": True})
                if target:
                    builder.add_link(link_attrs.get("display", ""), target, provenance={"sheet": attrs.get("name", "")})
            dimension = next((item for item in root.iter() if local_name(item.tag) == "dimension"), None)
            used_range = _attrs(dimension).get("ref", "") if dimension is not None else ""
            comments = []
            tables = []
            charts = []
            for relationship in sheet_rels.values():
                rel_type = relationship.get("type", "").casefold()
                target = relationship.get("target", "")
                if rel_type.endswith("/comments") and container.has(target):
                    comments_root = container.xml(target)
                    for comment in _descendants(comments_root, "comment"):
                        comments.append({"ref": _attrs(comment).get("ref", ""), "text": _text(comment)})
                elif rel_type.endswith("/table") and container.has(target):
                    table_root = container.xml(target)
                    table_attrs = _attrs(table_root)
                    tables.append({"name": table_attrs.get("name", ""), "ref": table_attrs.get("ref", "")})
                elif rel_type.endswith("/chart"):
                    charts.append({"chart_ref": f"chart_{len(charts) + 1:06d}", "metadata_only": True})
            builder.add_sheet({
                "name": attrs.get("name", ""),
                "state": attrs.get("state", "visible"),
                "used_range": used_range,
                "cells": cells,
                "merged_ranges": merged,
                "hidden_rows": hidden_rows,
                "hidden_columns": hidden_columns,
                "comments": comments,
                "hyperlinks": hyperlinks,
                "structured_tables": tables,
                "charts": charts,
            })
        return builder.finish()


def _ods_cell_value(cell) -> dict:
    attrs = _attrs(cell)
    value_type = attrs.get("value-type", "string")
    formula = attrs.get("formula", "")
    value = (
        attrs.get("string-value") or attrs.get("date-value") or
        attrs.get("time-value") or attrs.get("boolean-value") or
        attrs.get("value") or _text(cell)
    )
    result: dict[str, Any] = {"cell_type": value_type, "value": clean_text(value)}
    if formula:
        result.update({"formula": clean_text(formula), "cached_value": clean_text(value), "formula_inert": True})
    annotations = [_text(item) for item in _descendants(cell, "annotation") if _text(item)]
    if annotations:
        result["comments"] = annotations
    links = []
    for link in _descendants(cell, "a"):
        href = _attrs(link).get("href", "")
        if href:
            links.append({"target": href, "text": _text(link), "inert": True})
    if links:
        result["hyperlinks"] = links
    return result


def _extract_ods(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("ods", "spreadsheet")
        for warning in warnings:
            builder.warn(warning)
        root = container.xml("content.xml")
        named = []
        for item in root.iter():
            if local_name(item.tag) in {"named-range", "named-expression"}:
                attrs = _attrs(item)
                named.append({
                    "name": attrs.get("name", "")[:256],
                    "reference": (attrs.get("cell-range-address") or attrs.get("expression") or "")[:4_096],
                })
        builder.metadata["named_ranges"] = named[:50_000]
        for table in [item for item in root.iter() if local_name(item.tag) == "table"]:
            attrs = _attrs(table)
            cells = []
            hidden_rows = []
            hidden_columns = []
            merged = []
            row_number = 0
            declared_column = 0
            for child in list(table):
                tag = local_name(child.tag)
                child_attrs = _attrs(child)
                if tag == "table-column":
                    repeat = min(int(child_attrs.get("number-columns-repeated", "1") or 1), MAX_COLUMN)
                    if child_attrs.get("visibility") in {"collapse", "filter"}:
                        hidden_columns.append({"start": declared_column + 1, "count": repeat})
                    declared_column += repeat
                    continue
                if tag != "table-row":
                    continue
                repeat_rows = int(child_attrs.get("number-rows-repeated", "1") or 1)
                if repeat_rows > MAX_ROW or row_number + repeat_rows > MAX_ROW:
                    raise StructuredDocumentError("resource_limit_exceeded", limit_kind="worksheet_coordinate")
                row_cells = [
                    cell for cell in list(child)
                    if local_name(cell.tag) in {"table-cell", "covered-table-cell"}
                ]
                row_has_values = any(
                    any(_ods_cell_value(cell).get(key) not in {None, ""} for key in ("value", "formula"))
                    or bool(_ods_cell_value(cell).get("comments"))
                    for cell in row_cells
                )
                if not row_has_values:
                    if child_attrs.get("visibility") in {"collapse", "filter"}:
                        hidden_rows.append({"start": row_number + 1, "count": repeat_rows})
                    row_number += repeat_rows
                    continue
                populated_per_row = 0
                for cell in row_cells:
                    cell_attrs = _attrs(cell)
                    base = _ods_cell_value(cell)
                    if any(base.get(key) not in {None, ""} for key in ("value", "formula")) or base.get("comments"):
                        populated_per_row += int(
                            cell_attrs.get("number-columns-repeated", "1") or 1
                        )
                if len(cells) + populated_per_row * repeat_rows > MAX_POPULATED_CELLS:
                    raise StructuredDocumentError(
                        "resource_limit_exceeded", limit_kind="populated_cells",
                    )
                for _ in range(repeat_rows):
                    row_number += 1
                    if child_attrs.get("visibility") in {"collapse", "filter"}:
                        hidden_rows.append(row_number)
                    column = 0
                    for cell in list(child):
                        if local_name(cell.tag) not in {"table-cell", "covered-table-cell"}:
                            continue
                        cell_attrs = _attrs(cell)
                        repeat = int(cell_attrs.get("number-columns-repeated", "1") or 1)
                        if repeat > MAX_COLUMN or column + repeat > MAX_COLUMN:
                            raise StructuredDocumentError("resource_limit_exceeded", limit_kind="worksheet_coordinate")
                        base = _ods_cell_value(cell)
                        has_value = any(base.get(key) not in {None, ""} for key in ("value", "formula")) or base.get("comments")
                        if not has_value:
                            column += repeat
                        else:
                            for _column_repeat in range(repeat):
                                column += 1
                                coordinate = _number_coordinate(row_number, column)
                                cells.append({**base, "coordinate": coordinate, "row": row_number, "column": column})
                        if cell_attrs.get("number-columns-spanned") or cell_attrs.get("number-rows-spanned"):
                            merged.append({
                                "start": _number_coordinate(row_number, max(1, column - repeat + 1)),
                                "columns": int(cell_attrs.get("number-columns-spanned", "1") or 1),
                                "rows": int(cell_attrs.get("number-rows-spanned", "1") or 1),
                            })
            builder.add_sheet({
                "name": attrs.get("name", ""),
                "state": "hidden" if (
                    attrs.get("visibility") in {"collapse", "filter"}
                    or attrs.get("display", "true").casefold() == "false"
                ) else "visible",
                "used_range": _used_range(cells),
                "cells": cells,
                "merged_ranges": merged,
                "hidden_rows": hidden_rows,
                "hidden_columns": hidden_columns,
                "comments": [], "hyperlinks": [], "structured_tables": [], "charts": [],
            })
        return builder.finish()


def _number_coordinate(row: int, column: int) -> str:
    if row < 1 or row > MAX_ROW or column < 1 or column > MAX_COLUMN:
        raise StructuredDocumentError("resource_limit_exceeded", limit_kind="worksheet_coordinate")
    letters = ""
    value = column
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


def _used_range(cells: list[dict]) -> str:
    if not cells:
        return ""
    min_row = min(cell["row"] for cell in cells)
    max_row = max(cell["row"] for cell in cells)
    min_col = min(cell["column"] for cell in cells)
    max_col = max(cell["column"] for cell in cells)
    return f"{_number_coordinate(min_row, min_col)}:{_number_coordinate(max_row, max_col)}"


def _extract_delimited(data: bytes, format_label: str, warnings: tuple[str, ...]) -> dict:
    text, encoding, replaced = _decode_text(data)
    builder = DocumentBuilder(format_label, "spreadsheet")
    for warning in warnings:
        builder.warn(warning)
    if replaced:
        builder.warn("decoding_replacements")
    delimiter = "\t" if format_label == "tsv" else ","
    if format_label == "csv":
        try:
            dialect = csv.Sniffer().sniff(text[:128_000], delimiters=",;|")
            delimiter = dialect.delimiter
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = delimiter
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        cells = []
        max_columns = 0
        for row_index, row in enumerate(reader, start=1):
            if row_index > MAX_ROW:
                raise StructuredDocumentError("resource_limit_exceeded", limit_kind="worksheet_coordinate")
            max_columns = max(max_columns, len(row))
            if max_columns > MAX_COLUMN:
                raise StructuredDocumentError("resource_limit_exceeded", limit_kind="worksheet_coordinate")
            for column_index, value in enumerate(row, start=1):
                if value == "":
                    continue
                cell = {
                    "coordinate": _number_coordinate(row_index, column_index),
                    "row": row_index, "column": column_index,
                    "cell_type": "text", "value": value,
                }
                if value.startswith(("=", "+", "-", "@")):
                    cell["formula_like"] = True
                    cell["formula_inert"] = True
                cells.append(cell)
        builder.metadata.update({
            "encoding": encoding,
            "delimiter": delimiter,
            "row_count": row_index if "row_index" in locals() else 0,
            "column_count": max_columns,
        })
        builder.add_sheet({
            "name": "data", "state": "visible", "used_range": _used_range(cells),
            "cells": cells, "merged_ranges": [], "hidden_rows": [],
            "hidden_columns": [], "comments": [], "hyperlinks": [],
            "structured_tables": [], "charts": [],
        })
    except csv.Error as exc:
        raise StructuredDocumentError("corrupted_document") from exc
    return builder.finish()


def _ppt_text(shape) -> str:
    return clean_text("\n".join(
        _text(paragraph) for paragraph in _descendants(shape, "p") if _text(paragraph)
    ))


def _extract_pptx(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("pptx", "presentation")
        for warning in warnings:
            builder.warn(warning)
        relationships = _rels_by_source(container)
        presentation = container.xml("ppt/presentation.xml")
        presentation_rels = relationships.get("ppt/presentation.xml", {})
        slide_ids = _descendants(presentation, "sldId")
        for slide_number, slide_ref in enumerate(slide_ids, start=1):
            attrs = _attrs(slide_ref)
            rel = presentation_rels.get(attrs.get("id", ""))
            if not rel or not container.has(rel["target"]):
                raise StructuredDocumentError("corrupted_document")
            slide_name = rel["target"]
            root = container.xml(slide_name)
            slide_rels = relationships.get(slide_name, {})
            shapes = []
            tables = []
            links = []
            for element in root.iter():
                if local_name(element.tag) not in {"sp", "graphicFrame", "grpSp"}:
                    continue
                text = _ppt_text(element)
                shape_attrs = {}
                nv = next((item for item in element.iter() if local_name(item.tag) == "cNvPr"), None)
                if nv is not None:
                    shape_attrs = _attrs(nv)
                shape_id = f"shape_{len(shapes) + 1:06d}"
                shapes.append({
                    "shape_id": shape_id,
                    "ordinal": len(shapes),
                    "name": shape_attrs.get("name", "")[:256],
                    "text": text,
                    "grouped": local_name(element.tag) == "grpSp",
                })
                for hyperlink in _descendants(element, "hlinkClick"):
                    link_rel = slide_rels.get(_attrs(hyperlink).get("id", ""))
                    if link_rel:
                        links.append({"target": link_rel["target"], "inert": True, "shape_id": shape_id})
                        builder.add_link(text[:1_000], link_rel["target"], provenance={"slide": slide_number})
                for table in _descendants(element, "tbl"):
                    table_id = builder.add_table(
                        _xml_table_rows(table), provenance={"slide": slide_number},
                    )
                    tables.append(table_id)
            notes = ""
            comments = []
            charts = []
            for relationship in slide_rels.values():
                rel_type = relationship.get("type", "").casefold()
                target = relationship.get("target", "")
                if rel_type.endswith("/notesslide") and container.has(target):
                    notes = _ppt_text(container.xml(target))
                elif rel_type.endswith("/comments") and container.has(target):
                    for comment in _descendants(container.xml(target), "cm"):
                        comments.append(_text(comment))
                elif rel_type.endswith("/chart"):
                    charts.append({"chart_ref": f"chart_{len(charts) + 1:06d}", "metadata_only": True})
            builder.add_slide({
                "title": next((shape["text"] for shape in shapes if shape["text"]), "")[:1_000],
                "hidden": (
                    attrs.get("show") in {"0", "false"}
                    or _attrs(root).get("show") in {"0", "false"}
                ),
                "shapes": shapes, "tables": tables, "notes": notes,
                "comments": comments, "hyperlinks": links, "charts": charts,
                "master_theme_references": [
                    relationship["target"] for relationship in slide_rels.values()
                    if relationship.get("type", "").casefold().endswith(("/slidelayout", "/theme"))
                ][:16],
            })
        _record_media(builder, container, ("ppt/media/",))
        return builder.finish()


def _extract_odp(data: bytes, warnings: tuple[str, ...]) -> dict:
    with SafeZip(data) as container:
        builder = DocumentBuilder("odp", "presentation")
        for warning in warnings:
            builder.warn(warning)
        root = container.xml("content.xml")
        pages = [item for item in root.iter() if local_name(item.tag) == "page"]
        for page_number, page in enumerate(pages, start=1):
            attrs = _attrs(page)
            shapes = []
            tables = []
            notes = []
            links = []
            for table in _descendants(page, "table"):
                table_id = builder.add_table(
                    _xml_table_rows(table), provenance={"slide": page_number},
                )
                tables.append(table_id)
            for element in list(page):
                tag = local_name(element.tag)
                if tag == "notes":
                    notes.append(_text(element))
                    continue
                text = _text(element)
                if text:
                    shape_id = f"shape_{len(shapes) + 1:06d}"
                    shapes.append({
                        "shape_id": shape_id, "ordinal": len(shapes),
                        "name": _attrs(element).get("name", "")[:256],
                        "text": text,
                        "grouped": tag == "g",
                    })
                for link in _descendants(element, "a"):
                    target = _attrs(link).get("href", "")
                    if target:
                        links.append({"target": target, "inert": True})
                        builder.add_link(_text(link), target, provenance={"slide": page_number})
            builder.add_slide({
                "title": next((shape["text"] for shape in shapes if shape["text"]), "")[:1_000],
                "hidden": attrs.get("visibility") == "hidden",
                "shapes": shapes, "tables": tables,
                "notes": "\n".join(note for note in notes if note),
                "comments": [], "hyperlinks": links, "charts": [],
                "master_theme_references": [attrs.get("master-page-name", "")][:1],
            })
        _record_media(builder, container, ("Pictures/",))
        return builder.finish()


def extract_structured_document(data: bytes, format_label: str, warnings: tuple[str, ...] = ()) -> dict:
    """Dispatch already-inspected passive bytes to one bounded extractor."""

    try:
        if format_label == "docx":
            return _extract_docx(data, warnings)
        if format_label == "odt":
            return _extract_odt(data, warnings)
        if format_label == "rtf":
            return _extract_rtf(data, warnings)
        if format_label in {"txt", "markdown", "html"}:
            return _extract_text(data, format_label, warnings)
        if format_label == "epub":
            return _extract_epub(data, warnings)
        if format_label == "xlsx":
            return _extract_xlsx(data, warnings)
        if format_label == "ods":
            return _extract_ods(data, warnings)
        if format_label in {"csv", "tsv"}:
            return _extract_delimited(data, format_label, warnings)
        if format_label == "pptx":
            return _extract_pptx(data, warnings)
        if format_label == "odp":
            return _extract_odp(data, warnings)
        raise StructuredDocumentError("unsupported_format")
    except StructuredDocumentError:
        raise
    except ContainerError as exc:
        raise StructuredDocumentError(exc.category, limit_kind=exc.limit_kind) from exc
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise StructuredDocumentError("corrupted_document") from exc


__all__ = ["extract_structured_document"]
