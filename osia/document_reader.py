"""Bounded offline PDF/image extraction worker and document tool contract."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from osia import document_artifacts
from osia.runtime import DOCUMENT_WORKER_MODE, is_frozen


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
MAX_IMAGE_DIMENSION = 8_192
MAX_IMAGE_PIXELS = 16_000_000
MAX_OUTPUT_CHARS = document_artifacts.MAX_CONTENT_CHARS
MAX_OUTPUT_BYTES = document_artifacts.MAX_PAYLOAD_BYTES
DEFAULT_TIMEOUT_SECONDS = 25.0
MIN_TIMEOUT_SECONDS = 1.0
WORKER_JOB_RE = re.compile(r"^osia-document-job-[0-9a-f]{16}$")
_SAFE_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{2,8})?$")


class DocumentReadError(RuntimeError):
    """One stable, source-free document extraction failure."""

    def __init__(self, category: str, *, limit_kind: str | None = None):
        super().__init__(category)
        self.category = category
        self.limit_kind = limit_kind


@dataclass(frozen=True)
class SafeLimits:
    max_file_bytes: int = MAX_FILE_BYTES
    max_pages: int = MAX_PAGES
    max_pixels: int = MAX_IMAGE_PIXELS
    max_chars: int = MAX_OUTPUT_CHARS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def as_dict(self) -> dict:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_pages": self.max_pages,
            "max_pixels": self.max_pixels,
            "max_chars": self.max_chars,
            "timeout_seconds": self.timeout_seconds,
        }


def _bounded_int(value, default: int, ceiling: int, kind: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise DocumentReadError("limit_exceeded", limit_kind=kind)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DocumentReadError("limit_exceeded", limit_kind=kind) from exc
    if parsed < 1:
        raise DocumentReadError("limit_exceeded", limit_kind=kind)
    return min(parsed, ceiling)


def resolve_limits(
    *, max_file_bytes=None, max_pages=None, max_pixels=None,
    max_chars=None, timeout_seconds=None,
) -> SafeLimits:
    if timeout_seconds is None:
        timeout = DEFAULT_TIMEOUT_SECONDS
    else:
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise DocumentReadError(
                "limit_exceeded", limit_kind="execution_time",
            ) from exc
        if not math.isfinite(timeout) or timeout < MIN_TIMEOUT_SECONDS:
            raise DocumentReadError("limit_exceeded", limit_kind="execution_time")
        timeout = min(timeout, DEFAULT_TIMEOUT_SECONDS)
    return SafeLimits(
        max_file_bytes=_bounded_int(
            max_file_bytes, MAX_FILE_BYTES, MAX_FILE_BYTES, "file_size",
        ),
        max_pages=_bounded_int(max_pages, MAX_PAGES, MAX_PAGES, "page_count"),
        max_pixels=_bounded_int(
            max_pixels, MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS, "image_pixels",
        ),
        max_chars=_bounded_int(
            max_chars, MAX_OUTPUT_CHARS, MAX_OUTPUT_CHARS, "output_size",
        ),
        timeout_seconds=timeout,
    )


def _safe_language(value) -> str:
    if value is None or not str(value).strip():
        return "auto"
    text = str(value).strip()
    if not _SAFE_LANGUAGE_RE.fullmatch(text):
        raise DocumentReadError("unsupported_format")
    return text.lower().replace("_", "-")


def _document_type(data: bytes) -> str:
    prefix = data[:1024]
    if b"%PDF-" in prefix:
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    raise DocumentReadError("unsupported_format")


def _read_snapshot(path: str | Path, limit: int) -> tuple[bytes, str]:
    candidate = Path(path).expanduser()
    try:
        source = candidate.resolve(strict=True)
        with source.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise DocumentReadError("unsupported_format")
            if before.st_size <= 0:
                raise DocumentReadError("malformed_document")
            if before.st_size > limit:
                raise DocumentReadError("limit_exceeded", limit_kind="file_size")
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
    except DocumentReadError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DocumentReadError("extraction_failure") from exc
    if len(data) > limit:
        raise DocumentReadError("limit_exceeded", limit_kind="file_size")
    if before.st_size != after.st_size or len(data) != before.st_size:
        raise DocumentReadError("extraction_failure")
    return data, _document_type(data)


def _job_directory() -> Path:
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    for _ in range(20):
        directory = root / f"osia-document-job-{secrets.token_hex(8)}"
        try:
            directory.mkdir(mode=0o700)
            return directory
        except FileExistsError:
            continue
    raise DocumentReadError("extraction_failure")


def _private_write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _bounded_read(path: Path, limit: int, *, limit_kind="output_size") -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise DocumentReadError("extraction_failure") from exc
    if len(data) > limit:
        raise DocumentReadError("limit_exceeded", limit_kind=limit_kind)
    return data


def _safe_remove_job(directory: Path) -> None:
    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        target = directory.resolve(strict=False)
        if target.parent == root and WORKER_JOB_RE.fullmatch(target.name):
            shutil.rmtree(target)
    except OSError:
        pass


def _worker_command(directory: Path) -> list[str]:
    if is_frozen():
        return [sys.executable, DOCUMENT_WORKER_MODE, str(directory)]
    return [sys.executable, "-m", "osia.document_reader", "--worker", str(directory)]


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


def _run_worker(data: bytes, document_type: str, language: str,
                limits: SafeLimits) -> tuple[str, dict]:
    directory = _job_directory()
    try:
        _private_write(directory / "input.bin", data)
        request = {
            "schema_version": 1,
            "document_type": document_type,
            "language": language,
            "limits": limits.as_dict(),
        }
        _private_write(
            directory / "request.json",
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        process = subprocess.Popen(
            _worker_command(directory),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            return_code = process.wait(timeout=limits.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if _stop_worker(process):
                raise DocumentReadError(
                    "limit_exceeded", limit_kind="execution_time",
                ) from exc
            raise DocumentReadError("extraction_failure") from exc
        if return_code != 0:
            raise DocumentReadError("extraction_failure")
        try:
            result_raw = _bounded_read(
                directory / "result.json",
                document_artifacts.MAX_METADATA_BYTES,
            )
            result = json.loads(result_raw.decode("utf-8"))
        except DocumentReadError:
            raise
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise DocumentReadError("extraction_failure") from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            category = str(result.get("category") or "extraction_failure")
            if category not in {
                "unsupported_format", "encrypted_document", "malformed_document",
                "limit_exceeded", "ocr_unavailable", "ocr_failure",
                "extraction_failure",
            }:
                category = "extraction_failure"
            raise DocumentReadError(
                category,
                limit_kind=(
                    str(result.get("limit_kind"))
                    if result.get("limit_kind") else None
                ),
            )
        try:
            payload = _bounded_read(directory / "content.txt", MAX_OUTPUT_BYTES)
            content = payload.decode("utf-8")
        except DocumentReadError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise DocumentReadError("extraction_failure") from exc
        if len(content) > limits.max_chars:
            raise DocumentReadError("limit_exceeded", limit_kind="output_size")
        if hashlib.sha256(payload).hexdigest() != result.get("content_hash"):
            raise DocumentReadError("extraction_failure")
        if len(content) != result.get("content_length"):
            raise DocumentReadError("extraction_failure")
        return content, result
    finally:
        _safe_remove_job(directory)


def _error_result(error: DocumentReadError) -> dict:
    result = {"ok": False, "category": error.category}
    if error.limit_kind in {
        "file_size", "page_count", "image_pixels", "image_dimensions",
        "output_size", "execution_time", "artifact_count",
    }:
        result["limit_kind"] = error.limit_kind
    return result


def read_document(
    path: str,
    language: str | None = None,
    max_file_bytes=None,
    max_pages=None,
    max_pixels=None,
    max_chars=None,
    timeout_seconds=None,
) -> str:
    """Extract one local PDF/PNG/JPEG into a private artifact."""

    try:
        limits = resolve_limits(
            max_file_bytes=max_file_bytes,
            max_pages=max_pages,
            max_pixels=max_pixels,
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )
        language_value = _safe_language(language)
        data, document_type = _read_snapshot(path, limits.max_file_bytes)
        content, metadata = _run_worker(
            data, document_type, language_value, limits,
        )
        reference = document_artifacts.get_store().create(content, metadata)
        document_artifacts.capture_reference(reference)
        return json.dumps(
            {"ok": True, **reference},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except DocumentReadError as exc:
        return "ERROR: " + json.dumps(
            _error_result(exc), sort_keys=True, separators=(",", ":"),
        )
    except document_artifacts.ArtifactStoreError as exc:
        error = DocumentReadError(
            exc.category,
            limit_kind="artifact_count" if exc.category == "limit_exceeded" else None,
        )
        return "ERROR: " + json.dumps(
            _error_result(error), sort_keys=True, separators=(",", ":"),
        )
    except Exception:
        return 'ERROR: {"category":"extraction_failure","ok":false}'


def read_document_artifact(
    artifact_id: str,
    offset: int = 0,
    chunk_chars: int | None = None,
) -> str:
    """Return one authorized bounded chunk; never accepts a filesystem path."""

    try:
        result = document_artifacts.get_store().read_chunk(
            artifact_id, offset=offset, chunk_chars=chunk_chars,
        )
        reference = document_artifacts.get_store().inspect(artifact_id)
        document_artifacts.capture_reference(reference)
        return json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except document_artifacts.ArtifactStoreError as exc:
        return "ERROR: " + json.dumps(
            {"ok": False, "category": exc.category},
            sort_keys=True, separators=(",", ":"),
        )
    except Exception:
        return 'ERROR: {"category":"corrupted_artifact","ok":false}'


def _native_text_is_reliable(text: str) -> bool:
    clean = str(text or "").replace("\ufffd", "")
    visible = [char for char in clean if not char.isspace()]
    alnum = sum(char.isalnum() for char in visible)
    replacements = str(text or "").count("\ufffd")
    return alnum >= 1 and replacements <= max(1, len(visible) // 20)


def _check_image_limits(width: int, height: int, limits: SafeLimits) -> None:
    if width <= 0 or height <= 0:
        raise DocumentReadError("malformed_document")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise DocumentReadError("limit_exceeded", limit_kind="image_dimensions")
    if width * height > limits.max_pixels:
        raise DocumentReadError("limit_exceeded", limit_kind="image_pixels")


_OCR_ENGINE = None


def _ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        import rapidocr
        from rapidocr import RapidOCR

        model_root = Path(rapidocr.__file__).resolve().parent / "models"
        models = {
            "Det.model_path": model_root / "PP-OCRv6_det_small.onnx",
            "Cls.model_path": model_root / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
            "Rec.model_path": model_root / "PP-OCRv6_rec_small.onnx",
        }
        if any(not path.is_file() or path.stat().st_size <= 0 for path in models.values()):
            raise DocumentReadError("ocr_unavailable")
        params = {"Global.log_level": "critical"}
        params.update({key: str(path) for key, path in models.items()})
        _OCR_ENGINE = RapidOCR(params=params)
        return _OCR_ENGINE
    except DocumentReadError:
        raise
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise DocumentReadError("ocr_unavailable") from exc
    except Exception as exc:
        raise DocumentReadError("ocr_failure") from exc


def _ocr_image(image) -> tuple[str, list[float]]:
    try:
        import numpy as np

        rgb = image.convert("RGB")
        result = _ocr_engine()(np.asarray(rgb))
        texts = [str(item).strip() for item in (result.txts or ()) if str(item).strip()]
        scores = [
            float(item) for item in (result.scores or ())
            if isinstance(item, (int, float)) and math.isfinite(float(item))
        ]
    except DocumentReadError:
        raise
    except Exception as exc:
        raise DocumentReadError("ocr_failure") from exc
    if not texts:
        raise DocumentReadError("ocr_failure")
    return "\n".join(texts), scores


def _bounded_content(pages: list[str], limits: SafeLimits) -> str:
    content = "\n\n".join(
        f"--- Page {index} ---\n{text.strip()}"
        for index, text in enumerate(pages, start=1)
    ).strip()
    encoded = content.encode("utf-8")
    if len(content) > limits.max_chars or len(encoded) > MAX_OUTPUT_BYTES:
        raise DocumentReadError("limit_exceeded", limit_kind="output_size")
    return content


def _extract_image(data: bytes, document_type: str,
                   limits: SafeLimits) -> tuple[str, dict]:
    try:
        from PIL import Image, UnidentifiedImageError
    except (ImportError, ModuleNotFoundError) as exc:
        raise DocumentReadError("ocr_unavailable") from exc

    try:
        with Image.open(io.BytesIO(data)) as image:
            expected = "PNG" if document_type == "png" else "JPEG"
            if image.format != expected:
                raise DocumentReadError("malformed_document")
            _check_image_limits(image.width, image.height, limits)
            image.load()
            text, scores = _ocr_image(image)
    except DocumentReadError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise DocumentReadError("malformed_document") from exc
    content = _bounded_content([text], limits)
    return content, {
        "document_type": document_type,
        "page_count": 1,
        "page_methods": ["ocr"],
        "confidence": sum(scores) / len(scores) if scores else None,
        "warnings": [],
    }


def _pdfium_password_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "password" in text or "err_password" in text


def _extract_pdf(data: bytes, limits: SafeLimits) -> tuple[str, dict]:
    # Standard encrypted PDFs name an indirect encryption dictionary in the
    # trailer. Match that structure, not an arbitrary `/Encrypt` byte sequence
    # that may legitimately occur inside unencrypted page text/content.
    if re.search(rb"/Encrypt\s+\d+\s+\d+\s+R\b", data):
        raise DocumentReadError("encrypted_document")
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
    except (ImportError, ModuleNotFoundError) as exc:
        raise DocumentReadError("extraction_failure") from exc
    except Exception as exc:
        if _pdfium_password_error(exc):
            raise DocumentReadError("encrypted_document") from exc
        raise DocumentReadError("malformed_document") from exc
    pages: list[str] = []
    methods: list[str] = []
    scores: list[float] = []
    try:
        page_count = len(pdf)
        if page_count < 1:
            raise DocumentReadError("malformed_document")
        if page_count > limits.max_pages:
            raise DocumentReadError("limit_exceeded", limit_kind="page_count")
        for index in range(page_count):
            page = pdf[index]
            try:
                text_page = page.get_textpage()
                try:
                    native = text_page.get_text_range(errors="replace").strip()
                finally:
                    text_page.close()
                if _native_text_is_reliable(native):
                    pages.append(native)
                    methods.append("native")
                    continue
                width, height = page.get_size()
                scale = 150.0 / 72.0
                pixel_width = int(math.ceil(width * scale))
                pixel_height = int(math.ceil(height * scale))
                _check_image_limits(pixel_width, pixel_height, limits)
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    ocr_text, page_scores = _ocr_image(image)
                finally:
                    bitmap.close()
                pages.append(ocr_text)
                methods.append("ocr")
                scores.extend(page_scores)
            except DocumentReadError:
                raise
            except Exception as exc:
                raise DocumentReadError("extraction_failure") from exc
            finally:
                page.close()
    finally:
        pdf.close()
    content = _bounded_content(pages, limits)
    return content, {
        "document_type": "pdf",
        "page_count": len(pages),
        "page_methods": methods,
        "confidence": sum(scores) / len(scores) if scores else None,
        "warnings": [],
    }


def _worker_extract(request: dict, data: bytes) -> tuple[str, dict]:
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise DocumentReadError("extraction_failure")
    document_type = _document_type(data)
    if document_type != request.get("document_type"):
        raise DocumentReadError("malformed_document")
    raw_limits = request.get("limits") if isinstance(request.get("limits"), dict) else {}
    limits = resolve_limits(**raw_limits)
    if len(data) > limits.max_file_bytes:
        raise DocumentReadError("limit_exceeded", limit_kind="file_size")
    if document_type == "pdf":
        return _extract_pdf(data, limits)
    return _extract_image(data, document_type, limits)


def _worker_result(directory: Path, result: dict, content: str | None = None) -> None:
    if content is not None:
        payload = content.encode("utf-8")
        result = {
            "ok": True,
            **result,
            "content_length": len(content),
            "content_hash": hashlib.sha256(payload).hexdigest(),
        }
        _private_write(directory / "content.txt", payload)
    _private_write(
        directory / "result.json",
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def worker_main(job_path: str) -> int:
    """Run only against one random directory directly below the OS temp root."""

    directory: Path | None = None
    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        directory = Path(job_path).resolve(strict=True)
        if directory.parent != root or not WORKER_JOB_RE.fullmatch(directory.name):
            return 2
        request_raw = _bounded_read(
            directory / "request.json", document_artifacts.MAX_METADATA_BYTES,
        )
        request = json.loads(request_raw.decode("utf-8"))
        data = _bounded_read(
            directory / "input.bin", MAX_FILE_BYTES, limit_kind="file_size",
        )
        content, metadata = _worker_extract(request, data)
        _worker_result(directory, metadata, content)
        return 0
    except DocumentReadError as exc:
        try:
            if directory is None:
                return 1
            _worker_result(directory, _error_result(exc))
            return 0
        except Exception:
            return 1
    except Exception:
        try:
            if directory is None:
                return 1
            _worker_result(
                directory,
                {"ok": False, "category": "extraction_failure"},
            )
            return 0
        except Exception:
            return 1


def verify_packaged_dependencies() -> bool:
    """Offline import/model probe used by the frozen package smoke test."""

    try:
        import pypdfium2  # noqa: F401
        _ocr_engine()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2]))
    raise SystemExit(2)


__all__ = [
    "DocumentReadError",
    "SafeLimits",
    "read_document",
    "read_document_artifact",
    "resolve_limits",
    "verify_packaged_dependencies",
    "worker_main",
]
