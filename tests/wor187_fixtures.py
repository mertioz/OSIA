"""Deterministic, non-personal runtime fixture builders for WOR-187 tests."""

from __future__ import annotations

import io
import zipfile


FIXED_ZIP_TIME = (2024, 1, 1, 0, 0, 0)
LATE_MARKER = "WOR187_LATE_MARKER_Ω_最後"


def package(parts: dict[str, bytes | str], *, stored=()) -> bytes:
    output = io.BytesIO()
    stored_names = set(stored)
    with zipfile.ZipFile(output, "w") as archive:
        for name in sorted(parts):
            value = parts[name]
            payload = value.encode("utf-8") if isinstance(value, str) else value
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.create_system = 0
            info.external_attr = 0
            info.compress_type = (
                zipfile.ZIP_STORED if name in stored_names else zipfile.ZIP_DEFLATED
            )
            archive.writestr(info, payload)
    return output.getvalue()


ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{target}"/>
</Relationships>"""


def docx() -> bytes:
    return package({
        "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
        "_rels/.rels": ROOT_RELS.format(target="word/document.xml"),
        "word/_rels/document.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/inert" TargetMode="External"/>
<Relationship Id="rImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
</Relationships>""",
        "word/document.xml": f"""<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Titre 深い</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Élément liste</w:t></w:r></w:p>
<w:p><w:hyperlink r:id="rLink"><w:r><w:t>Lien passif</w:t></w:r></w:hyperlink></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Col A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Col B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:r><w:t>{LATE_MARKER}</w:t></w:r></w:p><w:sectPr/>
</w:body></w:document>""",
        "word/footnotes.xml": """<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:footnote w:id="1"><w:p><w:r><w:t>Note bas de page</w:t></w:r></w:p></w:footnote></w:footnotes>""",
        "word/endnotes.xml": """<w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:endnote w:id="1"><w:p><w:r><w:t>Note finale</w:t></w:r></w:p></w:endnote></w:endnotes>""",
        "word/comments.xml": """<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="1"><w:p><w:r><w:t>Commentaire</w:t></w:r></w:p></w:comment></w:comments>""",
        "word/header1.xml": """<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>En-tête</w:t></w:r></w:p></w:hdr>""",
        "word/footer1.xml": """<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Pied</w:t></w:r></w:p></w:ftr>""",
        "word/media/image1.png": b"\x89PNG\r\n\x1a\nfixture",
    })


ODF_MANIFEST = """<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" manifest:media-type="{mime}"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>"""


def odt() -> bytes:
    mime = "application/vnd.oasis.opendocument.text"
    return package({
        "mimetype": mime,
        "META-INF/manifest.xml": ODF_MANIFEST.format(mime=mime),
        "content.xml": f"""<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text>
<text:h text:outline-level="1">Titre ODT</text:h><text:list><text:list-item><text:p>Liste ODT</text:p></text:list-item></text:list>
<table:table table:name="T"><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell><table:table-cell><text:p>B</text:p></table:table-cell></table:table-row></table:table>
<office:annotation><text:p>Commentaire ODT</text:p></office:annotation><text:p>{LATE_MARKER}</text:p>
</office:text></office:body></office:document-content>""",
        "styles.xml": """<office:document-styles xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:master-styles><style:master-page><style:header><text:p>Header ODT</text:p></style:header><style:footer><text:p>Footer ODT</text:p></style:footer></style:master-page></office:master-styles></office:document-styles>""",
        "Pictures/image1.png": b"\x89PNG\r\n\x1a\nfixture",
    }, stored={"mimetype"})


def epub() -> bytes:
    return package({
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""",
        "OEBPS/book.opf": """<package xmlns="http://www.idpf.org/2007/opf"><metadata><title>EPUB Test</title><language>fr</language></metadata><manifest><item id="c1" href="chapter.xhtml" media-type="application/xhtml+xml"/><item id="img" href="image.png" media-type="image/png"/></manifest><spine><itemref idref="c1"/></spine></package>""",
        "OEBPS/chapter.xhtml": f"""<html xmlns="http://www.w3.org/1999/xhtml"><body><article><h1>Chapitre 一</h1><p>Texte EPUB</p><ul><li>Liste</li></ul><table><tr><td>A</td><td>B</td></tr></table><a href="https://example.invalid/inert">Lien</a><p>{LATE_MARKER}</p></article></body></html>""",
        "OEBPS/image.png": b"\x89PNG\r\n\x1a\nfixture",
    }, stored={"mimetype"})


def xlsx() -> bytes:
    return package({
        "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>""",
        "_rels/.rels": ROOT_RELS.format(target="xl/workbook.xml"),
        "xl/_rels/workbook.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rSheet1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rSheet2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>""",
        "xl/workbook.xml": """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Visible" sheetId="1" r:id="rSheet1"/><sheet name="VeryHidden" sheetId="2" state="veryHidden" r:id="rSheet2"/></sheets><definedNames><definedName name="NamedValue">Visible!$A$1</definedName></definedNames></workbook>""",
        "xl/sharedStrings.xml": f"""<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Bonjour 世界</t></si><si><t>{LATE_MARKER}</t></si></sst>""",
        "xl/styles.xml": """<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>""",
        "xl/worksheets/sheet1.xml": """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><dimension ref="A1:C3"/><cols><col min="2" max="2" hidden="1"/></cols><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><f>SUM(C1:C2)</f><v>3</v></c><c r="C1" s="1"><v>45292</v></c></row><row r="2" hidden="1"><c r="A2" t="inlineStr"><is><t>texte</t></is></c><c r="C2"><v>3</v></c></row><row r="3"><c r="A3" t="s"><v>1</v></c></row></sheetData><mergeCells><mergeCell ref="A2:B2"/></mergeCells><hyperlinks><hyperlink ref="A1" r:id="rLink" display="Lien"/></hyperlinks></worksheet>""",
        "xl/worksheets/_rels/sheet1.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rLink" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.invalid/inert" TargetMode="External"/><Relationship Id="rComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="../comments1.xml"/><Relationship Id="rTable" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/></Relationships>""",
        "xl/worksheets/sheet2.xml": """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1"/><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>caché</t></is></c></row></sheetData></worksheet>""",
        "xl/comments1.xml": """<comments xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><authors><author>A</author></authors><commentList><comment ref="A1" authorId="0"><text><t>Commentaire</t></text></comment></commentList></comments>""",
        "xl/tables/table1.xml": """<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" name="Table1" ref="A1:C3"/>""",
    })


def ods() -> bytes:
    mime = "application/vnd.oasis.opendocument.spreadsheet"
    return package({
        "mimetype": mime,
        "META-INF/manifest.xml": ODF_MANIFEST.format(mime=mime),
        "content.xml": f"""<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet><table:named-expressions><table:named-range table:name="Named" table:cell-range-address="Sheet1.A1"/></table:named-expressions><table:table table:name="Sheet1"><table:table-column/><table:table-column table:visibility="collapse"/><table:table-row><table:table-cell office:value-type="string"><text:p>Bonjour</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="3" table:formula="of:=SUM([.A1:.A2])"><text:p>3</text:p></table:table-cell></table:table-row><table:table-row table:visibility="collapse"><table:table-cell office:value-type="string"><text:p>{LATE_MARKER}</text:p><office:annotation><text:p>Note</text:p></office:annotation></table:table-cell></table:table-row></table:table><table:table table:name="Hidden" table:visibility="collapse"><table:table-row><table:table-cell office:value-type="string"><text:p>secret</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet></office:body></office:document-content>""",
    }, stored={"mimetype"})


def pptx() -> bytes:
    return package({
        "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>""",
        "_rels/.rels": ROOT_RELS.format(target="ppt/presentation.xml"),
        "ppt/_rels/presentation.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rSlide1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/><Relationship Id="rSlide2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/></Relationships>""",
        "ppt/presentation.xml": """<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rSlide1"/><p:sldId id="257" r:id="rSlide2" show="0"/></p:sldIdLst></p:presentation>""",
        "ppt/slides/slide1.xml": """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="1" name="Title"/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Présentation 世界</a:t></a:r></a:p></p:txBody></p:sp><p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>A</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>B</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>""",
        "ppt/slides/_rels/slide1.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/><Relationship Id="rImage" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/></Relationships>""",
        "ppt/notesSlides/notesSlide1.xml": """<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Notes orateur</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>""",
        "ppt/slides/slide2.xml": f"""<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:grpSp><p:sp><p:txBody><a:p><a:r><a:t>{LATE_MARKER}</a:t></a:r></a:p></p:txBody></p:sp></p:grpSp></p:spTree></p:cSld></p:sld>""",
        "ppt/media/image1.png": b"\x89PNG\r\n\x1a\nfixture",
    })


def odp() -> bytes:
    mime = "application/vnd.oasis.opendocument.presentation"
    return package({
        "mimetype": mime,
        "META-INF/manifest.xml": ODF_MANIFEST.format(mime=mime),
        "content.xml": f"""<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:presentation><draw:page draw:name="Slide1"><draw:frame draw:name="Title"><draw:text-box><text:p>Présentation ODP</text:p></draw:text-box></draw:frame><presentation:notes><text:p>Notes ODP</text:p></presentation:notes></draw:page><draw:page draw:name="Slide2" presentation:visibility="hidden"><draw:g><draw:frame><draw:text-box><text:p>{LATE_MARKER}</text:p></draw:text-box></draw:frame></draw:g></draw:page></office:presentation></office:body></office:document-content>""",
    }, stored={"mimetype"})


def text_formats() -> dict[str, bytes]:
    rtf_marker = "".join(
        char if ord(char) < 128 else f"\\u{ord(char) if ord(char) < 32768 else ord(char) - 65536}?"
        for char in LATE_MARKER
    )
    return {
        "rtf": (r"{\rtf1\ansi\pard Bonjour Unicode \u19990?\par " + rtf_marker + "}").encode("ascii"),
        "txt": ("Bonjour UTF-8\n\n" + LATE_MARKER).encode("utf-8"),
        "markdown": ("# Titre\n\n- Élément\n\n|A|B|\n|---|---|\n|1|2|\n\n" + LATE_MARKER).encode("utf-8"),
        "html": ("<html><body><main><h1>Titre</h1><p>Bonjour</p><ul><li>Élément</li></ul><table><tr><td>A</td><td>B</td></tr></table><a href='https://example.invalid/inert'>Lien</a><p>" + LATE_MARKER + "</p></main></body></html>").encode("utf-8"),
        "csv": ("name,value\nbonjour,1\nformula,=1+2\nlate," + LATE_MARKER + "\n").encode("utf-8"),
        "tsv": ("name\tvalue\nbonjour\t1\nlate\t" + LATE_MARKER + "\n").encode("utf-8"),
    }


__all__ = [
    "LATE_MARKER", "docx", "epub", "odp", "ods", "odt", "package",
    "pptx", "text_formats", "xlsx",
]
