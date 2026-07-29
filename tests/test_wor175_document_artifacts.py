"""WOR-175 bounded offline document extraction and private-store contracts."""

from __future__ import annotations

import io
import json
import os
import secrets
import stat
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from osia import document_artifacts, document_reader
from osia.autonomous import state
from osia.document_sources import DocumentSourceStore, set_source_store_for_testing


def _runtime_text(chars: int = 48) -> str:
    return secrets.token_hex((chars + 1) // 2)[:chars]


def _text_from_codepoints(values: list[int]) -> str:
    return "".join(chr(value) for value in values)


def _pdf(objects: dict[int, bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max(objects) + 1)
    for number in range(1, len(offsets)):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("ascii")
        out += objects[number] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(offsets)}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _text_stream(text: str) -> bytes:
    raw = text.encode("cp1252").replace(b"\\", b"\\\\")
    raw = raw.replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"BT /F1 12 Tf 72 720 Td (" + raw + b") Tj ET"


def _scan_jpeg(text: str, *, size=(1200, 600)) -> tuple[bytes, int, int]:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 52)
    except OSError:
        font = ImageFont.load_default(size=52)
    draw.text((50, 220), text, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=94)
    return output.getvalue(), image.width, image.height


def make_pdf(pages: list[tuple[str, str]]) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    next_id = 4
    page_ids = []
    for kind, text in pages:
        if kind == "text":
            content = _text_stream(text)
            content_id = next_id
            page_id = next_id + 1
            next_id += 2
            objects[content_id] = (
                f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                + content + b"\nendstream"
            )
            objects[page_id] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
                + f"{content_id} 0 R >>".encode("ascii")
            )
        else:
            jpeg, width, height = _scan_jpeg(text)
            image_id = next_id
            content_id = next_id + 1
            page_id = next_id + 2
            next_id += 3
            objects[image_id] = (
                f"<< /Type /XObject /Subtype /Image /Width {width} "
                f"/Height {height} /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                f"/Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode("ascii")
                + jpeg + b"\nendstream"
            )
            content = f"q 612 0 0 306 0 243 cm /Im0 Do Q".encode("ascii")
            objects[content_id] = (
                f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                + content + b"\nendstream"
            )
            objects[page_id] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /XObject << /Im0 "
                + f"{image_id} 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii")
            )
        page_ids.append(page_id)
    kids = " ".join(f"{item} 0 R" for item in page_ids)
    objects[2] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode("ascii")
    return _pdf(objects)


@pytest.fixture
def private_store(tmp_path):
    store = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    previous = document_artifacts.set_store_for_testing(store)
    try:
        yield store
    finally:
        document_artifacts.set_store_for_testing(previous)


def _read(path: Path, **kwargs) -> dict:
    result = document_reader.read_document(str(path), **kwargs)
    assert not result.startswith("ERROR:"), result
    return json.loads(result)


def _create(store, content=None, **metadata):
    if content is None:
        content = _runtime_text()
    values = {
        "document_type": "pdf",
        "page_count": 1,
        "page_methods": ["native"],
        "confidence": None,
        "warnings": [],
    }
    values.update(metadata)
    return store.create(content, values)


def test_text_unicode_multipage_pdf_native_hash_and_complete_chunks(
    tmp_path, private_store,
):
    unicode_probe = _text_from_codepoints([
        82, 233, 115, 117, 109, 233, 32, 102, 114, 97, 110, 231, 97, 105, 115,
        32, 110, 117, 109, 233, 114, 111, 32, 117, 110,
    ])
    tail_probe = _runtime_text()
    source = tmp_path / "source.pdf"
    source.write_bytes(make_pdf([
        ("text", unicode_probe),
        ("text", _runtime_text(500) + " " + tail_probe),
    ]))
    with document_artifacts.artifact_access_scope("text-run"):
        ref = _read(source)
        repeated = _read(source)
        assert ref["document_type"] == "pdf"
        assert ref["page_count"] == 2
        assert ref["page_methods"] == ["native", "native"]
        first = private_store.read_chunk(ref["artifact_id"], 0, 300)
        second = private_store.read_chunk(
            ref["artifact_id"], first["next_offset"], 800,
        )
    combined = first["content"] + second["content"]
    assert unicode_probe in combined
    assert tail_probe in combined
    assert second["eof_marker"] == "END_OF_DOCUMENT"
    assert ref["content_length"] == len(combined)
    assert "content_hash" not in ref
    assert repeated["artifact_id"] == ref["artifact_id"]
    assert repeated["content_length"] == ref["content_length"]


def test_scanned_and_mixed_pdf_use_ocr_only_where_needed(tmp_path, private_store):
    french_probe = _text_from_codepoints([
        82, 201, 83, 85, 77, 201, 32, 70, 82, 65, 78, 199, 65, 73, 83,
    ])
    native_probe = _text_from_codepoints([
        78, 65, 84, 73, 86, 69, 32, 83, 69, 78, 84, 73, 78, 69, 76,
    ])
    ocr_probe = _text_from_codepoints([
        77, 73, 88, 69, 68, 32, 79, 67, 82, 32, 83, 69, 78, 84, 73, 78, 69, 76,
    ])
    scanned = tmp_path / "scan.pdf"
    mixed = tmp_path / "mixed.pdf"
    scanned.write_bytes(make_pdf([("scan", french_probe)]))
    mixed.write_bytes(make_pdf([
        ("text", native_probe),
        ("scan", ocr_probe),
    ]))
    with document_artifacts.artifact_access_scope("ocr-run"):
        scanned_ref = _read(scanned)
        mixed_ref = _read(mixed)
        scanned_text = private_store.read_chunk(
            scanned_ref["artifact_id"], 0, 800,
        )["content"]
        mixed_text = private_store.read_chunk(
            mixed_ref["artifact_id"], 0, 1600,
        )["content"]
    assert scanned_ref["page_methods"] == ["ocr"]
    assert mixed_ref["page_methods"] == ["native", "ocr"]
    assert french_probe in scanned_text
    assert native_probe in mixed_text
    assert ocr_probe in mixed_text


@pytest.mark.parametrize("fmt,document_type", [("PNG", "png"), ("JPEG", "jpeg")])
def test_png_and_jpeg_contract(fmt, document_type, tmp_path, private_store, monkeypatch):
    extracted = _runtime_text()
    source = tmp_path / f"image.{fmt.lower()}"
    Image.new("RGB", (80, 40), "white").save(source, format=fmt)
    monkeypatch.setattr(
        document_reader, "_run_worker",
        lambda data, kind, language, limits: (
            extracted,
            {"document_type": kind, "page_count": 1,
             "page_methods": ["ocr"], "confidence": 0.9, "warnings": []},
        ),
    )
    with document_artifacts.artifact_access_scope("image-run"):
        ref = _read(source)
    assert ref["document_type"] == document_type
    assert ref["page_methods"] == ["ocr"]


def test_native_extraction_never_calls_ocr(monkeypatch):
    native_text = _runtime_text()
    monkeypatch.setattr(
        document_reader, "_ocr_image",
        lambda image: pytest.fail("OCR must not run on reliable native text"),
    )
    content, metadata = document_reader._extract_pdf(
        make_pdf([("text", native_text)]),
        document_reader.SafeLimits(),
    )
    assert native_text in content
    assert metadata["page_methods"] == ["native"]


@pytest.mark.parametrize(
    "payload,category",
    [(b"", "corrupted_document"),
     (b"%PDF-1.4\nnot valid", "corrupted_document")],
)
def test_safe_input_errors(payload, category, tmp_path, private_store):
    source = tmp_path / "bad.bin"
    source.write_bytes(payload)
    result = document_reader.read_document(str(source))
    assert json.loads(result.removeprefix("ERROR: "))["category"] == category


def test_encrypted_size_page_and_pixel_limits(tmp_path, private_store, monkeypatch):
    encrypted = tmp_path / "encrypted.pdf"
    encrypted.write_bytes(
        make_pdf([("text", _runtime_text())]) + b"\n/Encrypt 99 0 R",
    )
    too_many = tmp_path / "pages.pdf"
    too_many.write_bytes(make_pdf([
        ("text", _runtime_text()), ("text", _runtime_text()),
    ]))
    large = tmp_path / "large.png"
    Image.new("RGB", (50, 50), "white").save(large)
    too_wide = tmp_path / "wide.png"
    Image.new("RGB", (document_reader.MAX_IMAGE_DIMENSION + 1, 1), "white").save(too_wide)
    with document_artifacts.artifact_access_scope("limits"):
        encrypted_result = document_reader.read_document(str(encrypted))
        size_result = document_reader.read_document(
            str(too_many), max_file_bytes=10,
        )
        page_result = document_reader.read_document(str(too_many), max_pages=1)
        output_result = document_reader.read_document(
            str(too_many), max_chars=10,
        )
        pixel_result = document_reader.read_document(str(large), max_pixels=100)
        dimension_result = document_reader.read_document(str(too_wide))
    assert '"category":"encrypted_document"' in encrypted_result
    assert '"limit_kind":"file_size"' in size_result
    assert '"limit_kind":"page_count"' in page_result
    assert '"limit_kind":"output_size"' in output_result
    assert '"limit_kind":"image_pixels"' in pixel_result
    assert '"limit_kind":"image_dimensions"' in dimension_result


def test_timeout_ocr_unavailable_and_ocr_failure_are_explicit(
    tmp_path, private_store, monkeypatch,
):
    source = tmp_path / "doc.pdf"
    source.write_bytes(make_pdf([("text", _runtime_text())]))
    monkeypatch.setattr(
        document_reader, "_run_worker",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            document_reader.DocumentReadError(
                "limit_exceeded", limit_kind="execution_time",
            )
        ),
    )
    assert '"limit_kind":"execution_time"' in document_reader.read_document(str(source))
    image = Image.new("RGB", (20, 20), "white")
    png = io.BytesIO()
    image.save(png, "PNG")
    monkeypatch.setattr(
        document_reader, "_ocr_image",
        lambda value: (_ for _ in ()).throw(
            document_reader.DocumentReadError("ocr_unavailable")
        ),
    )
    with pytest.raises(document_reader.DocumentReadError, match="ocr_unavailable"):
        document_reader._extract_image(
            png.getvalue(), "png", document_reader.SafeLimits(),
        )
    monkeypatch.setattr(
        document_reader, "_ocr_image",
        lambda value: (_ for _ in ()).throw(
            document_reader.DocumentReadError("ocr_failure")
        ),
    )
    with pytest.raises(document_reader.DocumentReadError, match="ocr_failure"):
        document_reader._extract_image(
            png.getvalue(), "png", document_reader.SafeLimits(),
        )


def test_artifact_access_bounds_integrity_expiry_and_permissions(
    tmp_path, monkeypatch,
):
    now = [1000.0]
    store = document_artifacts.ArtifactStore(
        tmp_path / "store", clock=lambda: now[0],
    )
    with document_artifacts.artifact_access_scope("owner"):
        payload = _runtime_text(10)
        ref = _create(store, payload)
        artifact_id = ref["artifact_id"]
        assert store.read_chunk(artifact_id, 2, 4)["content"] == payload[2:6]
        with pytest.raises(document_artifacts.ArtifactStoreError, match="invalid_offset"):
            store.read_chunk(artifact_id, 99, 4)
        with pytest.raises(document_artifacts.ArtifactStoreError, match="invalid_chunk_size"):
            store.read_chunk(artifact_id, 0, document_artifacts.MAX_CHUNK_CHARS + 1)
        (store.root / artifact_id / "payload.txt").write_text(_runtime_text(), "utf-8")
        with pytest.raises(document_artifacts.ArtifactStoreError, match="corrupted_artifact"):
            store.inspect(artifact_id)
    with pytest.raises(document_artifacts.ArtifactStoreError, match="invalid_artifact_id"):
        store.read_chunk("../secret")
    with document_artifacts.artifact_access_scope("owner", ["doc_" + "a" * 32]):
        with pytest.raises(document_artifacts.ArtifactStoreError, match="missing_artifact"):
            store.inspect("doc_" + "a" * 32)
    with document_artifacts.artifact_access_scope("expiry"):
        expiring = _create(store, _runtime_text())
        now[0] += document_artifacts.ARTIFACT_TTL_SECONDS + 1
        with pytest.raises(document_artifacts.ArtifactStoreError, match="expired_artifact"):
            store.inspect(expiring["artifact_id"])
    with document_artifacts.artifact_access_scope("unreadable"):
        unreadable = _create(store, _runtime_text())
        with monkeypatch.context() as patcher:
            patcher.setattr(
                store, "_read_bounded",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    PermissionError("denied")
                ),
            )
            with pytest.raises(
                document_artifacts.ArtifactStoreError,
                match="unreadable_artifact",
            ):
                store.inspect(unreadable["artifact_id"])
    if os.name != "nt":
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700


def test_unauthorized_atomic_count_metadata_and_orphan_cleanup(
    tmp_path, monkeypatch,
):
    store = document_artifacts.ArtifactStore(tmp_path / "store")
    with document_artifacts.artifact_access_scope("owner"):
        ref = _create(store)
    with document_artifacts.artifact_access_scope("other", [ref["artifact_id"]]):
        with pytest.raises(document_artifacts.ArtifactStoreError, match="unauthorized_artifact"):
            store.inspect(ref["artifact_id"])
    failing = document_artifacts.ArtifactStore(tmp_path / "atomic")
    monkeypatch.setattr(document_artifacts.os, "replace", lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        _create(failing)
    assert not list(failing.root.glob("doc_*"))
    monkeypatch.undo()
    monkeypatch.setattr(document_artifacts, "MAX_ARTIFACTS", 1)
    capped = document_artifacts.ArtifactStore(tmp_path / "capped")
    _create(capped)
    with pytest.raises(document_artifacts.ArtifactStoreError, match="limit_exceeded"):
        _create(capped)
    bounded = _create(
        document_artifacts.ArtifactStore(tmp_path / "metadata"),
        warnings=[f"warning_{index}" for index in range(30)],
    )
    assert len(bounded["warnings"]) == 8
    assert document_artifacts.sanitize_reference({"artifact_id": "bad"}) is None
    orphaned = document_artifacts.ArtifactStore(tmp_path / "orphaned")
    orphan = _create(orphaned)
    orphaned._live_ids.clear()
    monkeypatch.setattr(orphaned, "_creator_alive", lambda metadata: False)
    orphaned.cleanup(remove_unreferenced=True)
    assert not (orphaned.root / orphan["artifact_id"]).exists()


def test_checkpoint_lifecycle_backward_compatibility_and_privacy(
    tmp_path, private_store, monkeypatch,
):
    checkpoint = tmp_path / "task_state.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", checkpoint)
    st = state.new_task_state("document workflow", "provider", "model", [
        {"name": "phase", "tasks": [{"objective": "read"}]},
    ])
    private_path = str(tmp_path / "identity-bearing-name.pdf")
    private_text = _runtime_text()
    with document_artifacts.artifact_access_scope(st["task_id"]):
        ref = _create(private_store, private_text)
    st["artifacts"] = [ref]
    st["phases"][0]["tasks"][0]["artifact_ids"] = [ref["artifact_id"]]
    state.save_task_state(st)
    raw = checkpoint.read_text("utf-8")
    assert private_path not in raw
    assert private_text not in raw
    assert state.load_task_state()["artifacts"][0]["artifact_id"] == ref["artifact_id"]
    st["status"] = "in_progress"
    state.save_task_state(st)
    assert (private_store.root / ref["artifact_id"]).exists()
    st["status"] = "aborted"
    state.save_task_state(st)
    assert not (private_store.root / ref["artifact_id"]).exists()
    checkpoint.write_text(json.dumps({
        **state.new_task_state("old", "p", "m", [{"name": "p", "tasks": []}]),
    }), "utf-8")
    loaded = state.load_task_state()
    assert loaded["artifacts"] == []


def test_autonomous_document_paths_are_runtime_only(tmp_path, monkeypatch):
    checkpoint = tmp_path / "task_state.json"
    monkeypatch.setattr(state, "TASK_STATE_FILE", checkpoint)
    private_path = str(tmp_path / f"{_runtime_text()}.pdf")
    objective = f'Read "{private_path}" locally'
    st = state.new_task_state(objective, "p", "m", [{
        "name": "read",
        "tasks": [{
            "objective": f'Extract "{private_path}"',
            "success_criterion": f'Content from "{private_path}" is available',
        }],
    }])
    task = st["phases"][0]["tasks"][0]
    assert private_path not in st["objective"]
    assert private_path not in state.redact_document_paths(
        f"Read {private_path} locally",
    )
    assert task["_runtime_objective"].endswith(f'"{private_path}"')
    state.save_task_state(st)
    raw = checkpoint.read_text("utf-8")
    assert private_path not in raw
    assert "_runtime_objective" not in raw


def test_agent_registration_history_privacy_read_file_and_progress(tmp_path):
    from osia import agent

    plain = tmp_path / "plain.txt"
    plain.write_text("ordinary text", "utf-8")
    assert agent.tool_read_file(str(plain)) == "ordinary text"
    assert agent.TOOL_MAP["read_document"] is agent.tool_read_document
    assert agent.TOOL_MAP["read_document_artifact"] is agent.tool_read_document_artifact
    private = {"role": "assistant", "content": "secret", "_document_private": True}
    assert agent._sanitize_persisted_history_message(private) is None
    ref = {"artifact_id": "doc_" + "a" * 32}
    first = agent._document_progress_fingerprint(ref)
    second = agent._document_progress_fingerprint(ref)
    assert first == second and len(first) == 64


def test_normal_agent_tool_pipeline_keeps_path_and_payload_out_of_logs(
    tmp_path, private_store, capsys,
):
    from osia import agent

    payload = _runtime_text()
    source = tmp_path / f"{_runtime_text()}.pdf"
    source.write_bytes(make_pdf([("text", payload)]))
    source_store = DocumentSourceStore(tmp_path / "sources")
    previous = set_source_store_for_testing(source_store)
    try:
        with document_artifacts.artifact_access_scope("normal-chat"):
            capability = source_store.create_for_current_owner(
                source, source_kind="test_attachment",
            )
            result, is_error = agent._execute_tool_with_retry(
                "read_document", {"source_ref": capability["source_ref"]},
            )
            assert is_error is False
            ref = json.loads(result)
            chunk, chunk_error = agent._execute_tool_with_retry(
                "read_document_artifact",
                {"artifact_id": ref["artifact_id"], "offset": 0, "chunk_chars": 400},
            )
    finally:
        set_source_store_for_testing(previous)
    assert chunk_error is False
    assert payload in json.loads(chunk)["content"]
    output = capsys.readouterr().out
    assert str(source) not in output
    assert payload not in output


def test_packaging_declares_offline_pdf_ocr_dependencies():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text("utf-8")
    spec = (root / "packaging" / "windows" / "osia.spec").read_text("utf-8")
    launcher = (root / "packaging" / "windows" / "launcher.py").read_text("utf-8")
    assert "pypdfium2==5.12.1" in requirements
    assert "rapidocr==3.9.2" in requirements
    assert all(f'"{name}"' in spec for name in ("pypdfium2", "rapidocr", "onnxruntime"))
    assert "verify_packaged_dependencies" in launcher
    assert "DOCUMENT_WORKER_MODE" in launcher
