"""Bounded, local PDF rendering for the authoritative ``render_pdf`` tool.

Only structured content accepted by :mod:`osia.tool_contracts` reaches this
module.  Rendering runs in a short-lived child process, uses ReportLab
flowables/drawing primitives only, and verifies the result with the bundled
PDFium before an atomic no-overwrite publication.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping

from osia.runtime import PDF_RENDER_WORKER_MODE, is_frozen


MAX_STRUCTURED_BYTES = 1024 * 1024
MAX_BLOCKS = 200
MAX_PAGES = 100
MAX_RENDER_SECONDS = 25.0
MAX_WORKER_MEMORY_BYTES = 512 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_TITLE_CHARS = 500
MAX_FILENAME_CHARS = 120
MAX_LIST_ITEMS = 100
MAX_TABLE_ROWS = 100
MAX_TABLE_COLUMNS = 12
MAX_CELL_CHARS = 4_000
MAX_DIAGRAM_NODES = 12
MAX_DIAGRAM_EDGES = 20

_JOB_RE = re.compile(r"^osia-pdf-job-[0-9a-f]{16}$")
_TEMP_RE = re.compile(r"^\.[^/\\]{1,160}\.[0-9a-f]{16}\.tmp$")
_SAFE_RESULT_KEYS = frozenset({
    "status", "filename", "destination", "byte_size", "page_count",
    "verified",
})
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


class PdfRenderError(RuntimeError):
    """Stable, value-free PDF rendering failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code or "render_failure")


def normalize_pdf_filename(value: str) -> str:
    """Return one safe basename with a canonical ``.pdf`` extension."""

    if not isinstance(value, str):
        raise PdfRenderError("invalid_filename")
    filename = value.strip()
    if not filename or len(filename) > MAX_FILENAME_CHARS:
        raise PdfRenderError("invalid_filename")
    if filename in {".", ".."} or any(
        marker in filename for marker in ("/", "\\", ":", "\x00")
    ):
        raise PdfRenderError("invalid_filename")
    if any(ord(char) < 32 for char in filename) or filename[-1] in {".", " "}:
        raise PdfRenderError("invalid_filename")
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    else:
        filename = filename[:-4] + ".pdf"
    stem = filename[:-4].rstrip(". ")
    if not stem or stem.upper() in _WINDOWS_RESERVED:
        raise PdfRenderError("invalid_filename")
    if len(filename) > MAX_FILENAME_CHARS:
        raise PdfRenderError("invalid_filename")
    return filename


def _windows_desktop() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # FOLDERID_Desktop = B4BFCC3A-DB2C-424C-B029-7FE99A87C641.
        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folder_id = _GUID(
            0xB4BFCC3A, 0xDB2C, 0x424C,
            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr),
        )
        if result != 0 or not path_ptr.value:
            return None
        try:
            return Path(path_ptr.value)
        finally:
            ole32.CoTaskMemFree(path_ptr)
    except Exception:
        return None


def _xdg_desktop(home: Path) -> Path | None:
    if not sys.platform.startswith("linux"):
        return None
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    config = config_root / "user-dirs.dirs"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r'XDG_DESKTOP_DIR="([^"]+)"', line.strip())
            if not match:
                continue
            raw = match.group(1)
            if raw == "$HOME":
                return home
            if raw.startswith("$HOME/") and ".." not in Path(raw[6:]).parts:
                return home / raw[6:]
    except OSError:
        pass
    return None


def resolve_destination(label: str) -> tuple[str, Path]:
    """Resolve one model-visible destination label to a local directory."""

    if label != "desktop":
        raise PdfRenderError("invalid_destination")
    home = Path.home().resolve()
    candidates = [
        _windows_desktop(),
        _xdg_desktop(home),
        home / "Desktop",
        home / "Bureau",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir():
            return "Desktop", resolved
    raise PdfRenderError("destination_unavailable")


def _validate_structured_request(arguments: Mapping[str, Any]) -> dict[str, Any]:
    from osia.tool_contracts import get_tool_spec, validate_arguments

    spec = get_tool_spec("render_pdf")
    if spec is None:
        raise PdfRenderError("contract_unavailable")
    try:
        clean = validate_arguments(spec.input_schema, dict(arguments))
    except Exception as exc:
        raise PdfRenderError("invalid_arguments") from exc
    encoded = json.dumps(
        clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_STRUCTURED_BYTES:
        raise PdfRenderError("structured_input_limit")
    blocks = clean.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BLOCKS:
        raise PdfRenderError("invalid_blocks")
    visible = False
    for block in blocks:
        if not isinstance(block, dict):
            raise PdfRenderError("invalid_blocks")
        kind = block.get("type")
        if kind in {"heading", "paragraph"}:
            visible = visible or bool(str(block.get("text") or "").strip())
        elif kind in {"bullet_list", "numbered_list"}:
            items = block.get("items")
            if not isinstance(items, list) or len(items) > MAX_LIST_ITEMS:
                raise PdfRenderError("invalid_blocks")
            visible = visible or any(str(item).strip() for item in items)
        elif kind == "table":
            headers = block.get("headers")
            rows = block.get("rows")
            if (
                not isinstance(headers, list)
                or not 1 <= len(headers) <= MAX_TABLE_COLUMNS
                or not isinstance(rows, list)
                or len(rows) > MAX_TABLE_ROWS
                or any(not isinstance(row, list) or len(row) != len(headers) for row in rows)
            ):
                raise PdfRenderError("invalid_table")
            visible = True
        elif kind == "flow_diagram":
            nodes = block.get("nodes")
            edges = block.get("edges")
            if (
                not isinstance(nodes, list)
                or not 1 <= len(nodes) <= MAX_DIAGRAM_NODES
                or not isinstance(edges, list)
                or len(edges) > MAX_DIAGRAM_EDGES
            ):
                raise PdfRenderError("invalid_diagram")
            node_ids = [str(node.get("id") or "") for node in nodes if isinstance(node, dict)]
            if len(node_ids) != len(nodes) or len(set(node_ids)) != len(node_ids):
                raise PdfRenderError("invalid_diagram")
            known = set(node_ids)
            if any(
                not isinstance(edge, dict)
                or edge.get("from") not in known
                or edge.get("to") not in known
                for edge in edges
            ):
                raise PdfRenderError("invalid_diagram")
            visible = True
        elif kind != "page_break":
            raise PdfRenderError("invalid_blocks")
    if not visible:
        raise PdfRenderError("empty_content")
    return clean


def _job_directory() -> Path:
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    for _ in range(20):
        candidate = root / f"osia-pdf-job-{secrets.token_hex(8)}"
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            continue
    raise PdfRenderError("worker_unavailable")


def _private_write(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise PdfRenderError("worker_result_invalid")
    return payload


def _safe_remove_job(directory: Path) -> None:
    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        target = directory.resolve(strict=False)
        if target.parent == root and _JOB_RE.fullmatch(target.name):
            shutil.rmtree(target)
    except OSError:
        pass


def _worker_command(directory: Path) -> list[str]:
    if is_frozen():
        return [sys.executable, PDF_RENDER_WORKER_MODE, str(directory)]
    return [sys.executable, "-m", "osia.pdf_renderer", "--worker", str(directory)]


def _stop_worker(process: subprocess.Popen) -> bool:
    try:
        process.terminate()
        process.wait(timeout=3)
        return True
    except Exception:
        try:
            process.kill()
            process.wait(timeout=3)
            return True
        except Exception:
            return False


def _verify_pdf(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size <= 5:
            raise PdfRenderError("verification_failed")
        with path.open("rb") as handle:
            signature = handle.read(5)
        if signature != b"%PDF-":
            raise PdfRenderError("verification_failed")
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        has_text = False
        try:
            page_count = len(pdf)
            if not 1 <= page_count <= MAX_PAGES:
                raise PdfRenderError("page_limit")
            for index in range(page_count):
                page = pdf[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        if text_page.get_text_range(errors="replace").strip():
                            has_text = True
                    finally:
                        text_page.close()
                finally:
                    page.close()
        finally:
            pdf.close()
        if not has_text:
            raise PdfRenderError("empty_verified_content")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return {
            "byte_size": size,
            "page_count": page_count,
            "integrity_sha256": digest.hexdigest(),
        }
    except PdfRenderError:
        raise
    except Exception as exc:
        raise PdfRenderError("verification_failed") from exc


def _flow_diagram_flowable(block: Mapping[str, Any], available_width: float):
    from reportlab.lib import colors
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Flowable

    nodes = list(block["nodes"])
    edges = list(block["edges"])
    columns = min(3, max(1, int(math.ceil(math.sqrt(len(nodes))))))
    rows = int(math.ceil(len(nodes) / columns))
    gap_x = 18.0
    gap_y = 54.0
    node_width = max(104.0, (available_width - gap_x * (columns - 1)) / columns)
    node_height = 54.0
    height = rows * node_height + max(0, rows - 1) * gap_y + 18.0

    class _FlowDiagram(Flowable):
        def __init__(self):
            super().__init__()
            self.width = available_width
            self.height = height

        def wrap(self, avail_width, _avail_height):
            return min(self.width, avail_width), self.height

        def draw(self):
            canvas = self.canv
            positions: dict[str, tuple[float, float]] = {}
            for index, node in enumerate(nodes):
                row, column = divmod(index, columns)
                x = column * (node_width + gap_x)
                y = self.height - 9.0 - (row + 1) * node_height - row * gap_y
                positions[str(node["id"])] = (x, y)

            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor("#64748B"))
            canvas.setFillColor(colors.HexColor("#64748B"))
            canvas.setLineWidth(1.2)
            for edge in edges:
                start = positions[str(edge["from"])]
                end = positions[str(edge["to"])]
                x1, y1 = start[0] + node_width / 2, start[1] + node_height / 2
                x2, y2 = end[0] + node_width / 2, end[1] + node_height / 2
                angle = math.atan2(y2 - y1, x2 - x1)
                radius = min(node_width, node_height) * 0.46
                sx, sy = x1 + math.cos(angle) * radius, y1 + math.sin(angle) * radius
                ex, ey = x2 - math.cos(angle) * radius, y2 - math.sin(angle) * radius
                canvas.line(sx, sy, ex, ey)
                arrow = 6.0
                for delta in (2.55, -2.55):
                    canvas.line(
                        ex, ey,
                        ex + arrow * math.cos(angle + delta),
                        ey + arrow * math.sin(angle + delta),
                    )
                label = str(edge.get("label") or "").strip()
                if label:
                    label = label[:80]
                    width = stringWidth(label, "Helvetica", 7)
                    canvas.setFillColor(colors.white)
                    canvas.rect((sx + ex) / 2 - width / 2 - 2,
                                (sy + ey) / 2 - 3, width + 4, 9,
                                stroke=0, fill=1)
                    canvas.setFillColor(colors.HexColor("#475569"))
                    canvas.setFont("Helvetica", 7)
                    canvas.drawCentredString((sx + ex) / 2, (sy + ey) / 2 - 1, label)
                    canvas.setFillColor(colors.HexColor("#64748B"))

            for node in nodes:
                x, y = positions[str(node["id"])]
                canvas.setFillColor(colors.HexColor("#EFF6FF"))
                canvas.setStrokeColor(colors.HexColor("#2563EB"))
                canvas.roundRect(x, y, node_width, node_height, 7, stroke=1, fill=1)
                text = str(node["text"])
                words = text.split()
                lines: list[str] = []
                current = ""
                for word in words:
                    candidate = f"{current} {word}".strip()
                    if stringWidth(candidate, "Helvetica-Bold", 8.5) <= node_width - 14:
                        current = candidate
                    else:
                        if current:
                            lines.append(current)
                        current = word
                if current:
                    lines.append(current)
                lines = lines[:4]
                canvas.setFillColor(colors.HexColor("#0F172A"))
                canvas.setFont("Helvetica-Bold", 8.5)
                baseline = y + node_height / 2 + (len(lines) - 1) * 5
                for line in lines:
                    canvas.drawCentredString(x + node_width / 2, baseline, line)
                    baseline -= 10
            canvas.restoreState()

    return _FlowDiagram()


def _render_reportlab(request: Mapping[str, Any], temporary_path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        KeepTogether,
        ListFlowable,
        ListItem,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    pagesize = A4 if request["page_size"] == "A4" else LETTER
    title = str(request["title"])
    doc = SimpleDocTemplate(
        str(temporary_path),
        pagesize=pagesize,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=22 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="OSIA",
        subject="Locally generated structured PDF",
        pageCompression=1,
    )
    sample = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle(
            "OSIA_Title", parent=sample["Title"], fontName="Helvetica-Bold",
            fontSize=23, leading=28, textColor=colors.HexColor("#0F172A"),
            spaceAfter=16, alignment=TA_LEFT,
        ),
        "h1": ParagraphStyle(
            "OSIA_H1", parent=sample["Heading1"], fontName="Helvetica-Bold",
            fontSize=17, leading=21, textColor=colors.HexColor("#1D4ED8"),
            spaceBefore=12, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "OSIA_H2", parent=sample["Heading2"], fontName="Helvetica-Bold",
            fontSize=13.5, leading=17, textColor=colors.HexColor("#0F172A"),
            spaceBefore=9, spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "OSIA_H3", parent=sample["Heading3"], fontName="Helvetica-Bold",
            fontSize=11.5, leading=15, textColor=colors.HexColor("#334155"),
            spaceBefore=7, spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "OSIA_Body", parent=sample["BodyText"], fontName="Helvetica",
            fontSize=10.2, leading=14.5, textColor=colors.HexColor("#1E293B"),
            spaceAfter=7, splitLongWords=True,
        ),
        "cell": ParagraphStyle(
            "OSIA_Cell", parent=sample["BodyText"], fontName="Helvetica",
            fontSize=8.3, leading=10.5, textColor=colors.HexColor("#1E293B"),
            alignment=TA_LEFT, splitLongWords=True,
        ),
        "cell_header": ParagraphStyle(
            "OSIA_CellHeader", parent=sample["BodyText"], fontName="Helvetica-Bold",
            fontSize=8.3, leading=10.5, textColor=colors.white,
            alignment=TA_CENTER, splitLongWords=True,
        ),
    }

    def paragraph(text: Any, style: str = "body"):
        return Paragraph(html.escape(str(text)).replace("\n", "<br/>"), styles[style])

    story = [paragraph(title, "title"), Spacer(1, 2 * mm)]
    for block in request["blocks"]:
        kind = block["type"]
        if kind == "heading":
            story.append(paragraph(block["text"], f"h{int(block.get('level') or 1)}"))
        elif kind == "paragraph":
            story.append(paragraph(block["text"]))
        elif kind in {"bullet_list", "numbered_list"}:
            items = [
                ListItem(paragraph(item), leftIndent=5)
                for item in block["items"]
            ]
            list_options = {
                "bulletType": "1" if kind == "numbered_list" else "bullet",
                "leftIndent": 18,
                "bulletFontName": "Helvetica",
                "bulletFontSize": 9,
                "spaceAfter": 8,
            }
            if kind == "numbered_list":
                list_options["start"] = "1"
            story.append(ListFlowable(items, **list_options))
        elif kind == "table":
            headers = [paragraph(cell, "cell_header") for cell in block["headers"]]
            rows = [
                [paragraph(cell, "cell") for cell in row]
                for row in block["rows"]
            ]
            column_width = doc.width / len(headers)
            table = Table(
                [headers, *rows], colWidths=[column_width] * len(headers),
                repeatRows=1, hAlign="LEFT",
            )
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1D4ED8")),
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#CBD5E1")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                    colors.white, colors.HexColor("#F8FAFC"),
                ]),
            ]))
            story.extend([table, Spacer(1, 3 * mm)])
        elif kind == "page_break":
            story.append(PageBreak())
        elif kind == "flow_diagram":
            story.append(KeepTogether([
                _flow_diagram_flowable(block, doc.width),
                Spacer(1, 4 * mm),
            ]))

    def decorate_page(canvas, built_doc):
        canvas.saveState()
        width, height = pagesize
        canvas.setStrokeColor(colors.HexColor("#CBD5E1"))
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, height - 14 * mm, width - 18 * mm, height - 14 * mm)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.setFont("Helvetica", 7.5)
        header = title if len(title) <= 90 else title[:87] + "..."
        canvas.drawString(18 * mm, height - 11 * mm, header)
        canvas.drawRightString(width - 18 * mm, 10 * mm, f"{built_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)


def _atomic_no_overwrite(temporary_path: Path, final_path: Path) -> None:
    if sys.platform == "win32":
        os.rename(temporary_path, final_path)
        return
    os.link(temporary_path, final_path)
    temporary_path.unlink()


def _worker_result(directory: Path, result: Mapping[str, Any]) -> None:
    _private_write(
        directory / "result.json",
        json.dumps(
            dict(result), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def worker_main(job_path: str) -> int:
    """Render one validated request from a random OS-temp job directory."""

    directory: Path | None = None
    temporary_path: Path | None = None
    final_path: Path | None = None
    published_final = False
    try:
        if os.name != "nt":
            try:
                import resource

                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (MAX_WORKER_MEMORY_BYTES, MAX_WORKER_MEMORY_BYTES),
                )
            except Exception:
                pass
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        directory = Path(job_path).resolve(strict=True)
        if directory.parent != root or not _JOB_RE.fullmatch(directory.name):
            return 2
        raw = _bounded_read(directory / "request.json", MAX_STRUCTURED_BYTES + 4096)
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict) or request.get("schema_version") != 1:
            raise PdfRenderError("worker_request_invalid")
        arguments = _validate_structured_request(request.get("arguments") or {})
        destination = Path(request.get("destination_path") or "").resolve(strict=True)
        if not destination.is_dir() or not destination.is_absolute():
            raise PdfRenderError("destination_unavailable")
        filename = normalize_pdf_filename(arguments["filename"])
        temp_name = str(request.get("temporary_name") or "")
        if not _TEMP_RE.fullmatch(temp_name):
            raise PdfRenderError("worker_request_invalid")
        temporary_path = destination / temp_name
        final_path = destination / filename
        if final_path.exists():
            raise PdfRenderError("destination_exists")
        if temporary_path.exists():
            raise PdfRenderError("temporary_collision")
        _render_reportlab(arguments, temporary_path)
        verification = _verify_pdf(temporary_path)
        _atomic_no_overwrite(temporary_path, final_path)
        temporary_path = None
        published_final = True
        final_verification = _verify_pdf(final_path)
        if verification != final_verification:
            raise PdfRenderError("verification_failed")
        _worker_result(directory, {
            "status": "success_verified",
            "filename": filename,
            "destination": "Desktop",
            "byte_size": final_verification["byte_size"],
            "page_count": final_verification["page_count"],
            "verified": True,
            "integrity_sha256": final_verification["integrity_sha256"],
        })
        return 0
    except PdfRenderError as exc:
        try:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if published_final and final_path is not None:
                final_path.unlink(missing_ok=True)
            if directory is None:
                return 1
            _worker_result(directory, {
                "status": "failure", "reason_code": exc.code,
            })
            return 0
        except Exception:
            return 1
    except Exception:
        try:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if published_final and final_path is not None:
                final_path.unlink(missing_ok=True)
            if directory is None:
                return 1
            _worker_result(directory, {
                "status": "failure", "reason_code": "render_failure",
            })
            return 0
        except Exception:
            return 1


def _recover_final(final_path: Path, temporary_path: Path) -> dict[str, Any] | None:
    try:
        temporary_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not final_path.is_file():
        return None
    try:
        return _verify_pdf(final_path)
    except PdfRenderError:
        try:
            final_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _run_worker(arguments: dict[str, Any], destination: Path) -> dict[str, Any]:
    filename = normalize_pdf_filename(arguments["filename"])
    final_path = destination / filename
    if final_path.exists():
        raise PdfRenderError("destination_exists")
    temporary_name = f".{filename}.{secrets.token_hex(8)}.tmp"
    temporary_path = destination / temporary_name
    directory = _job_directory()
    process: subprocess.Popen | None = None
    try:
        request = {
            "schema_version": 1,
            "arguments": arguments,
            "destination_path": str(destination),
            "temporary_name": temporary_name,
        }
        _private_write(
            directory / "request.json",
            json.dumps(
                request, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        process = subprocess.Popen(
            _worker_command(directory),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        deadline = time.monotonic() + MAX_RENDER_SECONDS
        memory_process = None
        try:
            import psutil

            memory_process = psutil.Process(process.pid)
        except Exception:
            pass
        failure_code = "render_failure"
        while process.poll() is None:
            if time.monotonic() >= deadline:
                failure_code = "execution_time_limit"
                _stop_worker(process)
                break
            if memory_process is not None:
                try:
                    resident = int(memory_process.memory_info().rss)
                    resident += sum(
                        int(child.memory_info().rss)
                        for child in memory_process.children(recursive=True)
                    )
                    if resident > MAX_WORKER_MEMORY_BYTES:
                        failure_code = "worker_memory_limit"
                        _stop_worker(process)
                        break
                except Exception:
                    memory_process = None
            time.sleep(0.02)

        try:
            result = json.loads(
                _bounded_read(directory / "result.json", MAX_RESULT_BYTES).decode("utf-8")
            )
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("status") == "success_verified":
            safe = {key: result[key] for key in _SAFE_RESULT_KEYS if key in result}
            if (
                safe.get("filename") == filename
                and safe.get("destination") == "Desktop"
                and safe.get("verified") is True
                and type(safe.get("byte_size")) is int
                and type(safe.get("page_count")) is int
            ):
                return safe
            failure_code = "worker_result_invalid"
        elif isinstance(result, dict) and result.get("status") == "failure":
            failure_code = str(result.get("reason_code") or "render_failure")[:64]

        recovered = _recover_final(final_path, temporary_path)
        if recovered is not None:
            return {
                "status": "success_verified",
                "filename": filename,
                "destination": "Desktop",
                "byte_size": recovered["byte_size"],
                "page_count": recovered["page_count"],
                "verified": True,
            }
        raise PdfRenderError(failure_code)
    finally:
        if process is not None and process.poll() is None:
            _stop_worker(process)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        _safe_remove_job(directory)


def render_pdf(
    title: str,
    filename: str,
    destination: str,
    page_size: str,
    blocks: list[dict],
) -> str:
    """Render and verify one structured PDF, returning a typed safe receipt."""

    try:
        arguments = _validate_structured_request({
            "title": title,
            "filename": filename,
            "destination": destination,
            "page_size": page_size,
            "blocks": blocks,
        })
        _label, destination_path = resolve_destination(arguments["destination"])
        receipt = _run_worker(arguments, destination_path)
        return json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except PdfRenderError as exc:
        return "ERROR: " + json.dumps(
            {"status": "failure", "reason_code": exc.code},
            sort_keys=True, separators=(",", ":"),
        )
    except Exception:
        return 'ERROR: {"reason_code":"render_failure","status":"failure"}'


def verify_packaged_dependencies() -> bool:
    """Offline import/resource probe used by the frozen package smoke test."""

    try:
        import reportlab
        from reportlab.lib.pagesizes import A4  # noqa: F401
        from reportlab.platypus import SimpleDocTemplate  # noqa: F401
        import pypdfium2  # noqa: F401

        package_root = Path(reportlab.__file__).resolve().parent
        license_path = package_root.parent / "reportlab-4.4.10.dist-info" / "licenses" / "LICENSE"
        return reportlab.Version == "4.4.10" and license_path.is_file()
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2]))
    raise SystemExit(2)


__all__ = [
    "MAX_BLOCKS", "MAX_DIAGRAM_EDGES", "MAX_DIAGRAM_NODES", "MAX_PAGES",
    "MAX_RENDER_SECONDS", "MAX_STRUCTURED_BYTES", "MAX_WORKER_MEMORY_BYTES",
    "PdfRenderError", "normalize_pdf_filename", "render_pdf",
    "resolve_destination", "verify_packaged_dependencies", "worker_main",
]
