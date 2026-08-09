from __future__ import annotations

import json
import re
import stat
import struct
import io
import zipfile
import secrets
from pathlib import Path

import pytest

from osia import agent as agent_module
from osia import document_artifacts, document_reader
from osia import document_containers
from osia.autonomous import orchestrator, state
from osia.document_containers import (
    ContainerError, SafeZip, inspect_relationships, safe_xml_from_bytes,
)
from osia.document_identity import DocumentIdentityError, identify_document
from osia.document_sources import DocumentSourceStore, set_source_store_for_testing
from osia.tools.browser_artifacts import (
    BrowserArtifactStore,
    set_browser_artifact_store_for_testing,
)
from osia.tool_contracts import (
    ExecutionOutcome,
    ToolValidationError,
    get_tool_spec,
    static_tool_prompt_block,
    validate_arguments,
)
from tests.wor187_fixtures import (
    LATE_MARKER,
    docx,
    epub,
    odp,
    ods,
    odt,
    package,
    pptx,
    text_formats,
    xlsx,
)


@pytest.fixture
def private_stores(tmp_path):
    artifacts = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    sources = DocumentSourceStore(tmp_path / "sources")
    browser = BrowserArtifactStore(tmp_path / "browser")
    previous_artifacts = document_artifacts.set_store_for_testing(artifacts)
    previous_sources = set_source_store_for_testing(sources)
    previous_browser = set_browser_artifact_store_for_testing(browser)
    try:
        yield artifacts, sources, browser
    finally:
        document_artifacts.set_store_for_testing(previous_artifacts)
        set_source_store_for_testing(previous_sources)
        set_browser_artifact_store_for_testing(previous_browser)


FORMAT_BYTES = {
    "docx": docx,
    "odt": odt,
    "epub": epub,
    "xlsx": xlsx,
    "ods": ods,
    "pptx": pptx,
    "odp": odp,
}


@pytest.mark.parametrize("format_label", sorted(FORMAT_BYTES))
def test_container_formats_identity_parse_query_and_late_marker(
    format_label, tmp_path, private_stores,
):
    source = tmp_path / f"fixture.{format_label}"
    source.write_bytes(FORMAT_BYTES[format_label]())
    with document_artifacts.artifact_access_scope("structured-owner"):
        parsed = json.loads(document_reader.read_document(str(source)))
        assert parsed["ok"] is True
        assert parsed["schema_version"] == 2
        assert parsed["format_label"] == format_label
        assert "content_hash" not in parsed
        result = json.loads(document_reader.query_document(
            parsed["document_id"], "search", query=LATE_MARKER,
        ))
        assert result["ok"] is True
        assert result["not_found"] is False
        assert any(LATE_MARKER in item["snippet"] for item in result["items"])


@pytest.mark.parametrize("format_label", sorted(text_formats()))
def test_passive_text_formats_parse_and_query(
    format_label, tmp_path, private_stores,
):
    suffix = {"markdown": "md", "html": "html"}.get(format_label, format_label)
    source = tmp_path / f"fixture.{suffix}"
    source.write_bytes(text_formats()[format_label])
    with document_artifacts.artifact_access_scope("text-owner"):
        parsed = json.loads(document_reader.read_document(str(source)))
        assert parsed["format_label"] == format_label
        result = json.loads(document_reader.query_document(
            parsed["document_id"], "search", query=LATE_MARKER,
        ))
        assert result["not_found"] is False


def test_standalone_html_rejects_relative_resource_dependency():
    with pytest.raises(DocumentIdentityError, match="external_reference_detected"):
        identify_document(
            b'<html><body><img src="private-relative.png"></body></html>',
            display_name="fixture.html",
        )


def test_document_structure_queries(private_stores, tmp_path):
    fixtures = {
        "doc": ("docx", docx()),
        "sheet": ("xlsx", xlsx()),
        "deck": ("pptx", pptx()),
    }
    with document_artifacts.artifact_access_scope("query-owner"):
        refs = {}
        for key, (suffix, payload) in fixtures.items():
            path = tmp_path / f"{key}.{suffix}"
            path.write_bytes(payload)
            refs[key] = json.loads(document_reader.read_document(str(path)))["document_id"]
        headings = json.loads(document_reader.query_document(refs["doc"], "headings"))
        tables = json.loads(document_reader.query_document(refs["doc"], "table"))
        sheets = json.loads(document_reader.query_document(refs["sheet"], "sheets"))
        sheet_id = sheets["items"][0]["sheet_id"]
        cell_range = json.loads(document_reader.query_document(
            refs["sheet"], "range", sheet_id=sheet_id, cell_range_ref="A1:C3",
        ))
        formulas = json.loads(document_reader.query_document(
            refs["sheet"], "formula", sheet_id=sheet_id,
        ))
        slides = json.loads(document_reader.query_document(refs["deck"], "slides"))
        notes = json.loads(document_reader.query_document(refs["deck"], "notes"))
        embedded = json.loads(document_reader.query_document(refs["deck"], "embedded"))
    assert headings["items"][0]["text"] == "Titre 深い"
    assert tables["items"] and tables["items"][0]["rows"]
    assert sheets["items"][1]["state"] == "veryHidden"
    assert any(item["coordinate"] == "A3" for item in cell_range["items"])
    assert formulas["items"][0]["formula"] == "SUM(C1:C2)"
    assert len(slides["items"]) == 2 and slides["items"][1]["hidden"] is True
    assert notes["items"][0]["text"] == "Notes orateur"
    assert embedded["items"][0]["opaque"] is True


def test_ods_hidden_columns_keep_declared_coordinates(private_stores, tmp_path):
    path = tmp_path / "hidden.ods"
    path.write_bytes(ods())
    with document_artifacts.artifact_access_scope("ods-hidden-owner"):
        reference = json.loads(document_reader.read_document(str(path)))
        sheets = json.loads(document_reader.query_document(
            reference["document_id"], "sheets",
        ))
    assert sheets["items"][0]["hidden_columns"] == [{"start": 2, "count": 1}]
    assert sheets["items"][1]["state"] == "hidden"


def test_query_cursor_truncation_and_final_chunk_search(tmp_path, private_stores):
    source = tmp_path / "long.txt"
    source.write_text(("segment\n" * 6000) + LATE_MARKER, "utf-8")
    with document_artifacts.artifact_access_scope("cursor-owner"):
        ref = json.loads(document_reader.read_document(str(source)))
        first = json.loads(document_reader.query_document(
            ref["document_id"], "chunks", max_items=1, max_chars=8_000,
        ))
        assert first["truncated"] is True
        assert re.fullmatch(r"cursor_chunks_[0-9a-f]{8}", first["next_cursor"])
        late = json.loads(document_reader.query_document(
            ref["document_id"], "search", query=LATE_MARKER,
        ))
        tightly_bounded = json.loads(document_reader.query_document(
            ref["document_id"], "chunks", max_items=1, max_chars=1,
        ))
    assert late["items"][-1]["ordinal"] == ref["chunk_count"] - 1
    assert tightly_bounded["artifact_id"] == ref["document_id"]
    assert tightly_bounded["operation"] == "chunks"
    assert re.fullmatch(r"cursor_chunks_[0-9a-f]{8}", tightly_bounded["next_cursor"])
    assert sum(
        len(value) for item in tightly_bounded["items"]
        for value in item.values() if isinstance(value, str)
    ) <= 1


def test_scoped_deduplication_and_private_public_split(
    tmp_path, private_stores, monkeypatch,
):
    artifacts, _sources, _browser = private_stores
    source = tmp_path / "same.docx"
    source.write_bytes(docx())
    distinct_source = tmp_path / "distinct.docx"
    distinct_source.write_bytes(source.read_bytes())
    epoch = [7]
    monkeypatch.setattr(document_reader, "_document_policy_epoch", lambda: epoch[0])
    with document_artifacts.artifact_access_scope("dedupe-owner"):
        first = json.loads(document_reader.read_document(str(source)))
        second = json.loads(document_reader.read_document(str(source)))
        distinct = json.loads(document_reader.read_document(str(distinct_source)))
        epoch[0] = 8
        after_policy_change = json.loads(document_reader.read_document(str(source)))
        assert first["document_id"] == second["document_id"]
        assert distinct["document_id"] != first["document_id"]
        assert after_policy_change["document_id"] != first["document_id"]
        artifact_dir = artifacts.root / first["document_id"]
        public = json.loads((artifact_dir / "public.json").read_text("utf-8"))
        private = json.loads((artifact_dir / "private.json").read_text("utf-8"))
    assert "source_hash" not in public and "payload_hash" not in public
    assert "private_filename" not in public and "owner_hash" not in public
    assert re.fullmatch(r"[0-9a-f]{64}", private["source_hash"])
    assert not (artifact_dir / "metadata.json").exists()


def test_source_capability_attachment_and_browser_bridge(tmp_path, private_stores):
    _artifacts, sources, browser = private_stores
    source = tmp_path / "private-name.docx"
    source.write_bytes(docx())
    with document_artifacts.artifact_access_scope("source-owner"):
        capability = sources.create_for_current_owner(
            source, source_kind="test_attachment", display_name=source.name,
        )
        assert str(source) not in json.dumps(capability)
        parsed = json.loads(document_reader.read_document(source_ref=capability["source_ref"]))
        download_ref = browser.finalize_download(source, suggested_filename="download.docx")
        assert "sha256" not in download_ref
        bridged = json.loads(document_reader.read_document(source_ref=download_ref["artifact_id"]))
        result = json.loads(document_reader.query_document(
            bridged["document_id"], "search", query=LATE_MARKER,
        ))
    assert parsed["format_label"] == "docx"
    assert result["not_found"] is False


@pytest.mark.parametrize(
    "payload,name,category",
    [
        (b"MZ-not-an-executable", "renamed.docx", "active_content_detected"),
        (b"\x7fELF-not-an-executable", "renamed.pdf", "active_content_detected"),
        (b"#!/bin/sh\necho passive", "renamed.txt", "active_content_detected"),
        (b"plain text", "false.docx", "file_identity_mismatch"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy", "book.xls", "unsupported_legacy_format"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy", "deck.ppt", "unsupported_legacy_format"),
        (b"<html><script>bad()</script></html>", "active.html", "active_content_detected"),
        (b"{\\rtf1\\object passive}", "active.rtf", "active_content_detected"),
    ],
)
def test_identity_rejections(payload, name, category):
    with pytest.raises(DocumentIdentityError, match=category):
        identify_document(payload, display_name=name)


def test_container_and_xml_negative_policy():
    with pytest.raises(ContainerError, match="active_content_detected"):
        safe_xml_from_bytes(b"<!DOCTYPE x [<!ENTITY e 'x'>]><x>&e;</x>")
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(package({"../escape.xml": "<x/>"}))
    collision = package({"A.xml": "<x/>", "a.xml": "<x/>"})
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(collision)
    with pytest.raises(DocumentIdentityError, match="unsupported_format"):
        identify_document(package({"generic.txt": "not an office package"}), display_name="fake.docx")


def test_active_external_encrypted_and_drm_typed_rejections():
    macro = package({
        "[Content_Types].xml": "<Types>macroEnabled</Types>",
        "word/document.xml": "<document/>",
    })
    with pytest.raises(DocumentIdentityError, match="active_content_detected"):
        identify_document(macro, display_name="bad.docm")
    external = package({
        "[Content_Types].xml": "<Types/>",
        "_rels/.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>""",
        "word/document.xml": "<document/>",
        "word/_rels/document.xml.rels": """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="https://example.invalid/template" TargetMode="External"/></Relationships>""",
    })
    with pytest.raises(DocumentIdentityError, match="external_reference_detected"):
        identify_document(external, display_name="bad.docx")
    encrypted_odf = package({
        "mimetype": "application/vnd.oasis.opendocument.text",
        "META-INF/manifest.xml": "<manifest><encryption-data/></manifest>",
        "content.xml": "<document/>",
    }, stored={"mimetype"})
    with pytest.raises(DocumentIdentityError, match="encrypted_document"):
        identify_document(encrypted_odf, display_name="locked.odt")
    drm_epub = package({
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": "<container/>",
        "META-INF/encryption.xml": "<encryption/>",
    }, stored={"mimetype"})
    with pytest.raises(DocumentIdentityError, match="drm_protected"):
        identify_document(drm_epub, display_name="locked.epub")
    with pytest.raises(DocumentIdentityError, match="external_reference_detected"):
        identify_document(
            b"<html><img src=https://example.invalid/image.png></html>",
            display_name="remote.html",
        )
    javascript_link = package({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<document/>",
        "word/_rels/document.xml.rels": (
            '<Relationships><Relationship Id="r1" Type="urn/hyperlink" '
            'Target="javascript:bad()" TargetMode="External"/></Relationships>'
        ),
    })
    with pytest.raises(DocumentIdentityError, match="active_content_detected"):
        identify_document(javascript_link, display_name="active.docx")
    odf_javascript = package({
        "mimetype": "application/vnd.oasis.opendocument.text",
        "META-INF/manifest.xml": "<manifest/>",
        "content.xml": (
            '<document xmlns:xlink="urn:xlink"><a '
            'xlink:href="javascript:bad()">link</a></document>'
        ),
    }, stored={"mimetype"})
    with pytest.raises(DocumentIdentityError, match="active_content_detected"):
        identify_document(odf_javascript, display_name="active.odt")


def test_internal_identity_wins_advisory_extension_and_text_classification():
    office = identify_document(docx(), display_name="renamed.pdf")
    assert office.format_label == "docx"
    assert office.warnings == ("file_identity_mismatch",)
    csv_identity = identify_document(
        b"name,value\nalpha,1\nbeta,2\n", display_name="legacy.xls",
    )
    assert csv_identity.format_label == "csv"
    assert "file_identity_mismatch" in csv_identity.warnings
    html_identity = identify_document(
        b"<html><body><p>passive</p></body></html>", display_name="page.txt",
    )
    assert html_identity.format_label == "html"
    assert "file_identity_mismatch" in html_identity.warnings


def test_malformed_xml_broken_relationship_and_protection_are_typed():
    malformed = package({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<document>",
    })
    with pytest.raises(DocumentIdentityError, match="corrupted_document"):
        identify_document(malformed, display_name="bad.docx")
    entity = package({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<!DOCTYPE x><document/>",
    })
    with pytest.raises(DocumentIdentityError, match="active_content_detected"):
        identify_document(entity, display_name="entity.docx")
    broken = package({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<document/>",
        "word/_rels/document.xml.rels": (
            '<Relationships><Relationship Id="r1" Type="urn:test" '
            'Target="missing.xml"/></Relationships>'
        ),
    })
    with pytest.raises(DocumentIdentityError, match="corrupted_document"):
        identify_document(broken, display_name="broken.docx")
    protected = package({
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<document><documentProtection/></document>",
    })
    with pytest.raises(DocumentIdentityError, match="protected_document"):
        identify_document(protected, display_name="protected.docx")


@pytest.mark.parametrize(
    "payload,category",
    [
        (b"%PDF-1.4\n/JavaScript 1 0 R\n%%EOF", "active_content_detected"),
        (b"%PDF-1.4\n/Launch 1 0 R\n%%EOF", "active_content_detected"),
        (b"%PDF-1.4\n/EmbeddedFile 1 0 R\n%%EOF", "active_content_detected"),
        (b"%PDF-1.4\n/GoToR 1 0 R\n%%EOF", "external_reference_detected"),
        (b"%PDF-1.4\n/Encrypt 9 0 R\n%%EOF", "encrypted_document"),
    ],
)
def test_pdf_passive_preinspection(payload, category):
    with pytest.raises(DocumentIdentityError, match=category):
        identify_document(payload, display_name="unsafe.pdf")


def test_xlsx_invalid_shared_string_fails_without_partial_artifact(
    tmp_path, private_stores,
):
    payload = package({
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": (
            '<workbook xmlns:r="urn:r"><sheets><sheet name="Data" '
            'sheetId="1" r:id="r1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships><Relationship Id="r1" Type="urn:worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/sharedStrings.xml": "<sst><si><t>only</t></si></sst>",
        "xl/worksheets/sheet1.xml": (
            '<worksheet><sheetData><row r="1"><c r="A1" t="s">'
            '<v>99</v></c></row></sheetData></worksheet>'
        ),
    })
    path = tmp_path / "bad.xlsx"
    path.write_bytes(payload)
    artifacts = private_stores[0]
    with document_artifacts.artifact_access_scope("invalid-shared-string"):
        result = document_reader.read_document(str(path))
    assert json.loads(result.removeprefix("ERROR: "))["category"] == "corrupted_document"
    assert not any(item.name.startswith("doc_") for item in artifacts.root.glob("doc_*"))


def test_v2_expiry_authorization_corruption_and_query_states(tmp_path, monkeypatch):
    now = [100.0]
    store = document_artifacts.ArtifactStore(tmp_path / "artifacts", clock=lambda: now[0])
    previous = document_artifacts.set_store_for_testing(store)
    path = tmp_path / "fixture.docx"
    path.write_bytes(docx())
    try:
        with document_artifacts.artifact_access_scope("owner-a"):
            reference = json.loads(document_reader.read_document(str(path)))
            summary = json.loads(document_reader.query_document(reference["document_id"], "summary"))
            chunks = json.loads(document_reader.query_document(reference["document_id"], "chunks"))
            sections = json.loads(document_reader.query_document(reference["document_id"], "sections"))
            provenance = json.loads(document_reader.query_document(reference["document_id"], "provenance"))
            unsupported = json.loads(document_reader.query_document(reference["document_id"], "sheets"))
            missing = json.loads(document_reader.query_document(
                reference["document_id"], "table", table_id="table_999999",
            ))
        assert summary["summary"]["format_label"] == "docx"
        assert chunks["items"] and sections["items"] and provenance["items"]
        assert unsupported["unsupported"] is True
        assert missing["not_found"] is True
        with document_artifacts.artifact_access_scope("owner-b", [reference["document_id"]]):
            with pytest.raises(document_artifacts.ArtifactStoreError, match="unauthorized_artifact"):
                store.inspect(reference["document_id"])
        now[0] += document_artifacts.ARTIFACT_TTL_SECONDS + 1
        with document_artifacts.artifact_access_scope("owner-a", [reference["document_id"]]):
            with pytest.raises(document_artifacts.ArtifactStoreError, match="expired_artifact"):
                store.inspect(reference["document_id"])
    finally:
        document_artifacts.set_store_for_testing(previous)


def test_model_contract_advertises_exact_path_or_opaque_source_and_complete_query():
    read_spec = get_tool_spec("read_document")
    query_spec = get_tool_spec("query_document")
    assert set(read_spec.input_schema["properties"]) == {
        "path", "source_ref", "language", "max_file_bytes", "max_pages",
        "max_pixels", "max_chars", "timeout_seconds",
    }
    path_arguments = {"path": r"C:\Users\fixture\Desktop\document.docx"}
    source_arguments = {"source_ref": "src_" + "a" * 32}
    assert validate_arguments(read_spec.input_schema, path_arguments) == path_arguments
    assert validate_arguments(read_spec.input_schema, source_arguments) == source_arguments
    for invalid in (
        {}, {**path_arguments, **source_arguments},
        {"file_path": "document.docx"}, {"path": "relative/document.docx"},
    ):
        with pytest.raises(ToolValidationError, match="invalid_arguments"):
            validate_arguments(read_spec.input_schema, invalid)
    prompt = static_tool_prompt_block("en")
    read_line = next(line for line in prompt.splitlines() if line.startswith("- read_document:"))
    assert "(path:string; optional:source_ref:string" in read_line
    assert r'{"path":"C:\\Users\\Example\\Desktop\\report.pdf"}' in read_line
    assert "never shell, read_file, or render_pdf" in read_line
    assert query_spec.side_effect.value == "read_only"
    assert query_spec.idempotency.value == "idempotent"
    assert set(query_spec.input_schema["properties"]["operation"]["enum"]) == {
        "summary", "search", "chunks", "sections", "headings", "table",
        "sheets", "range", "formula", "slides", "notes", "embedded",
        "provenance", "metadata", "ocr", "frames", "pages", "transcript",
        "time_range", "timeline", "tracks", "chapters",
    }


def test_every_zip_resource_limit_is_bounded_with_small_payloads(monkeypatch):
    monkeypatch.setattr(document_containers, "MAX_ZIP_MEMBERS", 1)
    with pytest.raises(ContainerError) as error:
        SafeZip(package({"a.xml": "<a/>", "b.xml": "<b/>"}))
    assert error.value.limit_kind == "zip_member_count"
    monkeypatch.setattr(document_containers, "MAX_ZIP_MEMBERS", 5_000)
    monkeypatch.setattr(document_containers, "MAX_ZIP_UNCOMPRESSED_BYTES", 5)
    with pytest.raises(ContainerError) as error:
        SafeZip(package({"data.bin": b"123456"}, stored={"data.bin"}))
    assert error.value.limit_kind == "zip_uncompressed_size"
    monkeypatch.setattr(document_containers, "MAX_ZIP_UNCOMPRESSED_BYTES", 1_000)
    monkeypatch.setattr(document_containers, "MAX_NON_XML_MEMBER_BYTES", 5)
    with pytest.raises(ContainerError) as error:
        SafeZip(package({"data.bin": b"123456"}, stored={"data.bin"}))
    assert error.value.limit_kind == "zip_member_size"
    monkeypatch.setattr(document_containers, "MAX_NON_XML_MEMBER_BYTES", 1_000)
    monkeypatch.setattr(document_containers, "MAX_XML_MEMBER_BYTES", 4)
    with pytest.raises(ContainerError) as error:
        SafeZip(package({"data.xml": "<root/>"}, stored={"data.xml"}))
    assert error.value.limit_kind == "xml_part_size"
    monkeypatch.setattr(document_containers, "MAX_XML_MEMBER_BYTES", 1_000)
    monkeypatch.setattr(document_containers, "MAX_MEMBER_RATIO", 1.1)
    with pytest.raises(ContainerError) as error:
        SafeZip(package({"data.txt": "A" * 100}))
    assert error.value.limit_kind == "zip_member_ratio"
    monkeypatch.setattr(document_containers, "MAX_MEMBER_RATIO", 1_000.0)
    monkeypatch.setattr(document_containers, "MAX_AGGREGATE_RATIO", 1.1)
    with pytest.raises(ContainerError) as error:
        SafeZip(package({"a.txt": "A" * 100, "b.txt": "B" * 100}))
    assert error.value.limit_kind == "zip_aggregate_ratio"
    monkeypatch.setattr(document_containers, "MAX_AGGREGATE_RATIO", 1_000.0)
    nested = package({"inner.bin": package({"x.txt": "x"})}, stored={"inner.bin"})
    with pytest.raises(ContainerError) as error:
        SafeZip(nested)
    assert error.value.limit_kind == "nested_archive"


def test_zip_encryption_unknown_compression_symlink_duplicate_crc_and_truncation():
    base = bytearray(package({"data.bin": b"passive"}, stored={"data.bin"}))
    central = base.find(b"PK\x01\x02")
    encrypted = bytearray(base)
    struct.pack_into("<H", encrypted, 6, 1)
    struct.pack_into("<H", encrypted, central + 8, 1)
    with pytest.raises(ContainerError, match="encrypted_document"):
        SafeZip(bytes(encrypted))
    unknown = bytearray(base)
    struct.pack_into("<H", unknown, 8, 99)
    struct.pack_into("<H", unknown, central + 10, 99)
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(bytes(unknown))
    local_flag_mismatch = bytearray(base)
    struct.pack_into("<H", local_flag_mismatch, 6, 1)
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(bytes(local_flag_mismatch))
    local_method_mismatch = bytearray(base)
    struct.pack_into("<H", local_method_mismatch, 8, zipfile.ZIP_DEFLATED)
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(bytes(local_method_mismatch))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        link = zipfile.ZipInfo("link.xml", (2024, 1, 1, 0, 0, 0))
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "target")
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(stream.getvalue())
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(package({"dir//file.xml": "<x/>"}))
    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("same.xml", "<a/>")
        archive.writestr("same.xml", "<b/>")
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(duplicate.getvalue())
    normalized = package({"é.xml": "<a/>", "e\u0301.xml": "<b/>"})
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(normalized)
    corrupt = bytearray(base)
    filename_length = struct.unpack_from("<H", corrupt, 26)[0]
    extra_length = struct.unpack_from("<H", corrupt, 28)[0]
    data_offset = 30 + filename_length + extra_length
    corrupt[data_offset] ^= 0x01
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(bytes(corrupt))
    with pytest.raises(ContainerError, match="corrupted_document"):
        SafeZip(bytes(base[:-8]))


def test_every_xml_limit_is_bounded(monkeypatch):
    monkeypatch.setattr(document_containers, "MAX_XML_DEPTH", 2)
    with pytest.raises(ContainerError) as error:
        safe_xml_from_bytes(b"<a><b><c/></b></a>")
    assert error.value.limit_kind == "xml_depth"
    monkeypatch.setattr(document_containers, "MAX_XML_DEPTH", 128)
    monkeypatch.setattr(document_containers, "MAX_XML_ATTRIBUTES", 1)
    with pytest.raises(ContainerError) as error:
        safe_xml_from_bytes(b"<a b='1' c='2'/>")
    assert error.value.limit_kind == "xml_attribute_count"
    monkeypatch.setattr(document_containers, "MAX_XML_ATTRIBUTES", 256)
    monkeypatch.setattr(document_containers, "MAX_XML_NAME_LENGTH", 3)
    with pytest.raises(ContainerError) as error:
        safe_xml_from_bytes(b"<longname/>")
    assert error.value.limit_kind == "xml_name_length"
    monkeypatch.setattr(document_containers, "MAX_XML_NAME_LENGTH", 256)
    monkeypatch.setattr(document_containers, "MAX_XML_NAMESPACES", 1)
    with pytest.raises(ContainerError) as error:
        safe_xml_from_bytes(b"<a xmlns:x='urn:x' xmlns:y='urn:y'><x:b/><y:c/></a>")
    assert error.value.limit_kind == "xml_namespace_count"
    monkeypatch.setattr(document_containers, "MAX_XML_NAMESPACES", 64)
    duplicate_uri_declarations = (
        b"<a " + b" ".join(
            f"xmlns:n{index}='urn:same'".encode("ascii") for index in range(65)
        ) + b"/>"
    )
    with pytest.raises(ContainerError) as error:
        safe_xml_from_bytes(duplicate_uri_declarations)
    assert error.value.limit_kind == "xml_namespace_count"
    monkeypatch.setattr(document_containers, "MAX_NAMESPACE_URI_LENGTH", 5)
    with pytest.raises(ContainerError) as error:
        safe_xml_from_bytes(b"<a xmlns:x='urn:long'><x:b/></a>")
    assert error.value.limit_kind == "namespace_uri_length"


def test_relationship_edge_depth_and_cycle_limits(monkeypatch):
    relationship_xml = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{items}</Relationships>"""
    edges = package({
        "_rels/.rels": relationship_xml.format(items=(
            '<Relationship Id="r1" Type="urn:test" Target="a.xml"/>'
            '<Relationship Id="r2" Type="urn:test" Target="b.xml"/>'
        )),
        "a.xml": "<a/>", "b.xml": "<b/>",
    })
    monkeypatch.setattr(document_containers, "MAX_RELATIONSHIP_EDGES", 1)
    with SafeZip(edges) as container, pytest.raises(ContainerError) as error:
        inspect_relationships(container)
    assert error.value.limit_kind == "relationship_edges"
    monkeypatch.setattr(document_containers, "MAX_RELATIONSHIP_EDGES", 50_000)
    chain = package({
        "_rels/.rels": relationship_xml.format(items='<Relationship Id="r1" Type="urn:test" Target="a.xml"/>'),
        "_rels/a.xml.rels": relationship_xml.format(items='<Relationship Id="r2" Type="urn:test" Target="b.xml"/>'),
        "a.xml": "<a/>", "b.xml": "<b/>",
    })
    monkeypatch.setattr(document_containers, "MAX_RELATIONSHIP_DEPTH", 1)
    with SafeZip(chain) as container, pytest.raises(ContainerError) as error:
        inspect_relationships(container)
    assert error.value.limit_kind == "relationship_depth"
    monkeypatch.setattr(document_containers, "MAX_RELATIONSHIP_DEPTH", 32)
    cycle = package({
        "a/_rels/file.xml.rels": relationship_xml.format(items='<Relationship Id="r1" Type="urn:test" Target="../b/file.xml"/>'),
        "b/_rels/file.xml.rels": relationship_xml.format(items='<Relationship Id="r2" Type="urn:test" Target="../a/file.xml"/>'),
        "a/file.xml": "<a/>", "b/file.xml": "<b/>",
    })
    with SafeZip(cycle) as container, pytest.raises(ContainerError, match="corrupted_document"):
        inspect_relationships(container)


def test_autonomous_terminal_queries_without_reparse_and_emits_one_final(
    tmp_path, private_stores, monkeypatch,
):
    monkeypatch.setattr(state, "TASK_STATE_FILE", tmp_path / "task_state.json")
    source = tmp_path / "resume.docx"
    source.write_bytes(docx())
    run = state.new_task_state(
        "Find the late marker and give one consolidated answer.",
        "test", "test-model", [],
    )
    run["run_state"] = "ready"
    run["authorization_status"] = "authorized"
    run["authorization_source"] = "task_scoped"
    with document_artifacts.artifact_access_scope(run["task_id"]):
        reference = json.loads(document_reader.read_document(str(source)))
    run["artifacts"] = [reference]

    class FinalAgent:
        provider = "test"
        model = "test-model"
        last_run_diagnostic = None
        last_run_document_error = None

        def __init__(self):
            self.final_calls = 0
            self.last_run_document_activity = False

        def reset(self):
            self.last_run_document_activity = False

        def run(self, prompt):
            assert "query_document" in prompt
            document_id = re.search(r"doc_[0-9a-f]{32}", prompt).group(0)
            result = json.loads(document_reader.query_document(
                document_id, "search", query=LATE_MARKER,
            ))
            assert result["not_found"] is False
            self.final_calls += 1
            self.last_run_document_activity = True
            return "Consolidated document answer."

    monkeypatch.setattr(
        document_reader, "_run_worker",
        lambda *_args, **_kwargs: pytest.fail("document was reparsed on resume"),
    )
    events = []
    agent = FinalAgent()
    runner = orchestrator.Orchestrator(agent, events.append)
    runner._state = run
    assert runner._step(run) is False
    assert agent.final_calls == 1
    assert [event["event"] for event in events] == ["done"]
    assert events[0]["final_answer"] == "Consolidated document answer."
    assert "final_answer" not in json.dumps(run)


def test_mobile_autonomous_terminal_projects_only_bounded_sanitized_final():
    from osia.server import server

    run_id = "run-wor187-" + secrets.token_hex(8)
    run = {
        "run_id": run_id,
        "plan_revision": 1,
        "authorization_revision": 1,
        "status": "done",
        "run_state": "completed",
        "phases": [],
    }
    frame = server._autonomous_frame({
        "event": "done",
        "final_answer": "Safe answer C:\\private\\source.docx",
    }, run)
    assert frame["terminal"] is True
    assert frame["final_answer"] == "Safe answer [private local path]"
    assert "final_answer" not in server._autonomous_frame({
        "event": "running",
        "final_answer": "not terminal",
    }, {**run, "run_id": run_id + "-running", "status": "in_progress", "run_state": "running"})
