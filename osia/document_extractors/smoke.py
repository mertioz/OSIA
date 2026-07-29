"""Deterministic offline structured-document smoke for frozen Windows builds."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path


_MARKER = "WOR187_FROZEN_LATE_MARKER"
_ZIP_TIME = (2024, 1, 1, 0, 0, 0)
_OFFICE_PROCESSES = frozenset({
    "excel.exe", "winword.exe", "powerpnt.exe", "soffice.exe",
    "soffice.bin", "libreoffice.exe",
})


def _package(parts: dict[str, str | bytes], *, stored=()) -> bytes:
    stream = io.BytesIO()
    stored_names = set(stored)
    with zipfile.ZipFile(stream, "w") as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, _ZIP_TIME)
            info.compress_type = (
                zipfile.ZIP_STORED if name in stored_names
                else zipfile.ZIP_DEFLATED
            )
            value = parts[name]
            archive.writestr(
                info, value.encode("utf-8") if isinstance(value, str) else value,
            )
    return stream.getvalue()


def _fixtures() -> dict[str, bytes]:
    content_types = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    rels = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
    docx = _package({
        "[Content_Types].xml": content_types,
        "word/document.xml": (
            '<w:document xmlns:w="urn:w"><w:body><w:p><w:pPr>'
            '<w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Frozen DOCX</w:t>'
            f'</w:r></w:p><w:p><w:r><w:t>{_MARKER}</w:t></w:r></w:p>'
            '</w:body></w:document>'
        ),
    })
    odt = _package({
        "mimetype": "application/vnd.oasis.opendocument.text",
        "META-INF/manifest.xml": '<manifest xmlns="urn:manifest"/>',
        "content.xml": (
            '<office:document-content xmlns:office="urn:office" xmlns:text="urn:text">'
            '<office:body><office:text><text:h text:outline-level="1">Frozen ODT</text:h>'
            f'<text:p>{_MARKER}</text:p></office:text></office:body></office:document-content>'
        ),
    }, stored={"mimetype"})
    epub = _package({
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": (
            '<container xmlns="urn:container"><rootfiles><rootfile '
            'full-path="OEBPS/book.opf"/></rootfiles></container>'
        ),
        "OEBPS/book.opf": (
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata><title>Frozen EPUB</title>'
            '</metadata><manifest><item id="c1" href="chapter.xhtml" '
            'media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/>'
            '</spine></package>'
        ),
        "OEBPS/chapter.xhtml": f'<html><body><h1>Frozen EPUB</h1><p>{_MARKER}</p></body></html>',
    }, stored={"mimetype"})
    xlsx = _package({
        "[Content_Types].xml": content_types,
        "xl/workbook.xml": (
            '<workbook xmlns:r="urn:r"><sheets><sheet name="Data" sheetId="1" '
            'r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": rels.format(
            '<Relationship Id="rId1" Type="urn:worksheet" Target="worksheets/sheet1.xml"/>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<worksheet><dimension ref="A1:B2"/><sheetData><row r="1">'
            '<c r="A1" t="inlineStr"><is><t>Frozen XLSX</t></is></c>'
            '<c r="B1"><f>1+1</f><v>2</v></c></row><row r="2">'
            f'<c r="A2" t="inlineStr"><is><t>{_MARKER}</t></is></c>'
            '</row></sheetData></worksheet>'
        ),
    })
    ods = _package({
        "mimetype": "application/vnd.oasis.opendocument.spreadsheet",
        "META-INF/manifest.xml": '<manifest xmlns="urn:manifest"/>',
        "content.xml": (
            '<office:document-content xmlns:office="urn:office" xmlns:table="urn:table" '
            'xmlns:text="urn:text"><office:body><office:spreadsheet><table:table '
            'table:name="Data"><table:table-row><table:table-cell office:value-type="string">'
            '<text:p>Frozen ODS</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell office:value-type="string">'
            f'<text:p>{_MARKER}</text:p></table:table-cell></table:table-row>'
            '</table:table></office:spreadsheet></office:body></office:document-content>'
        ),
    }, stored={"mimetype"})
    pptx = _package({
        "[Content_Types].xml": content_types,
        "ppt/presentation.xml": (
            '<p:presentation xmlns:p="urn:p" xmlns:r="urn:r"><p:sldIdLst>'
            '<p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>'
        ),
        "ppt/_rels/presentation.xml.rels": rels.format(
            '<Relationship Id="rId1" Type="urn:slide" Target="slides/slide1.xml"/>'
        ),
        "ppt/slides/slide1.xml": (
            '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld><p:spTree><p:sp>'
            f'<p:txBody><a:p><a:r><a:t>Frozen PPTX {_MARKER}</a:t></a:r></a:p>'
            '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
        ),
    })
    odp = _package({
        "mimetype": "application/vnd.oasis.opendocument.presentation",
        "META-INF/manifest.xml": '<manifest xmlns="urn:manifest"/>',
        "content.xml": (
            '<office:document-content xmlns:office="urn:office" xmlns:draw="urn:draw" '
            'xmlns:text="urn:text"><office:body><office:presentation><draw:page '
            'draw:name="Slide 1"><draw:frame><draw:text-box><text:p>'
            f'Frozen ODP {_MARKER}</text:p></draw:text-box></draw:frame></draw:page>'
            '</office:presentation></office:body></office:document-content>'
        ),
    }, stored={"mimetype"})
    return {
        "docx": docx, "odt": odt, "epub": epub, "xlsx": xlsx,
        "ods": ods, "pptx": pptx, "odp": odp,
    }


def _pdf(text: str) -> bytes:
    escaped = text.encode("ascii").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    content = b"BT /F1 12 Tf 72 720 Td (" + escaped + b") Tj ET"
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Count 1 /Kids [5 0 R] >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
        5: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>",
    }
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0] * 6
    for number in range(1, 6):
        offsets[number] = len(output)
        output += f"{number} 0 obj\n".encode("ascii") + objects[number] + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 6\n0000000000 65535 f \n"
    output += b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:])
    output += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(output)


def _ocr_png() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1800, 500), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 92)
    except OSError:
        font = ImageFont.load_default(size=92)
    draw.text((60, 170), "OSIA OCR SMOKE 187", fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _office_processes() -> set[int]:
    try:
        import psutil

        return {
            int(process.pid)
            for process in psutil.process_iter(["name"])
            if str(process.info.get("name") or "").casefold() in _OFFICE_PROCESSES
        }
    except Exception:
        return set()


def run_packaged_document_smoke(root: Path) -> bool:
    """Parse/query all frozen critical formats without network or Office."""

    from osia import document_artifacts, document_reader

    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    before = _office_processes()
    store = document_artifacts.ArtifactStore(directory / "artifacts")
    previous = document_artifacts.set_store_for_testing(store)
    try:
        with document_artifacts.artifact_access_scope("frozen-document-smoke"):
            for label, payload in _fixtures().items():
                path = directory / f"fixture.{label}"
                path.write_bytes(payload)
                reference = json.loads(document_reader.read_document(str(path)))
                if reference.get("ok") is not True or reference.get("format_label") != label:
                    return False
                found = json.loads(document_reader.query_document(
                    reference["document_id"], "search", query=_MARKER,
                ))
                if found.get("not_found") is not False:
                    return False
                if label in {"xlsx", "ods"}:
                    sheets = json.loads(document_reader.query_document(
                        reference["document_id"], "sheets",
                    ))
                    sheet_id = sheets.get("items", [{}])[0].get("sheet_id")
                    ranged = json.loads(document_reader.query_document(
                        reference["document_id"], "range",
                        sheet_id=sheet_id, cell_range_ref="A1:B2",
                    ))
                    if not ranged.get("items"):
                        return False
                if label in {"pptx", "odp"}:
                    slides = json.loads(document_reader.query_document(
                        reference["document_id"], "slides",
                    ))
                    if not slides.get("items"):
                        return False

            pdf_path = directory / "native.pdf"
            pdf_path.write_bytes(_pdf("OSIA PDF SMOKE 187"))
            pdf_ref = json.loads(document_reader.read_document(str(pdf_path)))
            if pdf_ref.get("format_label") != "pdf" or pdf_ref.get("page_methods") != ["native"]:
                return False

            image_path = directory / "ocr.png"
            image_path.write_bytes(_ocr_png())
            image_ref = json.loads(document_reader.read_document(str(image_path)))
            if image_ref.get("format_label") != "png" or image_ref.get("page_methods") != ["ocr"]:
                return False
            ocr = json.loads(document_reader.query_document(
                image_ref["document_id"], "search", query="OSIA",
            ))
            if ocr.get("not_found") is not False:
                return False
        return not (_office_processes() - before)
    finally:
        document_artifacts.set_store_for_testing(previous)


__all__ = ["run_packaged_document_smoke"]
