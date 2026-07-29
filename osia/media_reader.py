"""Bounded local media extraction in a dedicated terminable worker.

The server process performs byte-first identity only. Pillow, PyAV, RapidOCR,
and faster-whisper are imported and invoked exclusively by the worker entry
point in this module. Worker inputs and outputs are private temporary files;
decoded frames and audio never leave the job directory.
"""

from __future__ import annotations

import hashlib
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
import wave
from pathlib import Path
from typing import Any

from osia.document_identity import DocumentIdentity, identify_document
from osia.document_models import CHUNK_CHARS, MAX_OUTPUT_BYTES
from osia.runtime import MEDIA_WORKER_MODE, bundled_model_path, is_frozen


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = MAX_OUTPUT_BYTES
MAX_WORKER_MEMORY_BYTES = 1536 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_CUMULATIVE_PIXELS = 64_000_000
MAX_IMAGE_FRAMES = 32
MAX_AUDIO_SECONDS = 3_600.0
MAX_AUDIO_CHANNELS = 8
MAX_AUDIO_SAMPLE_RATE = 192_000
MAX_AUDIO_TRACKS = 8
MAX_CHAPTERS = 256
MAX_TRANSCRIPT_SEGMENTS = 10_000
MAX_TRANSCRIPT_CHARS = 200_000
MAX_TRANSCRIPTION_WAV_BYTES = 128 * 1024 * 1024
MAX_VIDEO_SECONDS = 1_800.0
MAX_VIDEO_WIDTH = 3840
MAX_VIDEO_HEIGHT = 2160
MAX_VIDEO_TRACKS = 16
MAX_VIDEO_FRAMES = 60
MAX_VIDEO_FRAME_PIXELS = 240_000_000
IMAGE_TIMEOUT_SECONDS = 45.0
AUDIO_TIMEOUT_SECONDS = 120.0
VIDEO_TIMEOUT_SECONDS = 180.0
WORKER_JOB_RE = re.compile(r"^osia-media-job-[0-9a-f]{16}$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

IMAGE_FORMATS = frozenset({"png", "jpeg", "webp", "gif", "tiff", "bmp"})
AUDIO_FORMATS = frozenset({"wav", "mp3", "m4a", "aac", "ogg", "flac"})
VIDEO_FORMATS = frozenset({"mp4", "mov", "webm", "mkv"})
_AUDIO_CODECS = frozenset({
    "aac", "flac", "mp3", "opus", "vorbis", "pcm_s16le", "pcm_s24le",
    "pcm_s32le", "pcm_f32le", "pcm_f64le",
})
_VIDEO_CODECS = frozenset({
    "av1", "h264", "hevc", "mpeg4", "vp8", "vp9",
})


class MediaReadError(RuntimeError):
    """One stable path-free media outcome."""

    def __init__(
        self,
        category: str,
        *,
        limit_kind: str | None = None,
        stage: str | None = None,
    ):
        super().__init__(category)
        self.category = category
        self.limit_kind = limit_kind
        self.stage = stage if stage and _SAFE_CODE_RE.fullmatch(stage) else None


def _private_write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _bounded_read(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise MediaReadError("extraction_failure") from exc
    if len(data) > maximum:
        raise MediaReadError("resource_limit_exceeded", limit_kind="output_size")
    return data


def _job_directory() -> Path:
    root = Path(tempfile.gettempdir()).resolve(strict=True)
    for _ in range(20):
        directory = root / f"osia-media-job-{secrets.token_hex(8)}"
        try:
            directory.mkdir(mode=0o700)
            return directory
        except FileExistsError:
            continue
    raise MediaReadError("extraction_failure")


def cleanup_orphan_jobs(*, max_age_seconds: int = 3_600, maximum: int = 128) -> int:
    """Remove only stale OSIA media jobs directly below the OS temp root."""

    try:
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        entries = list(root.iterdir())[:maximum]
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for entry in entries:
        if not entry.is_dir() or not WORKER_JOB_RE.fullmatch(entry.name):
            continue
        try:
            if now - entry.stat().st_mtime < max_age_seconds:
                continue
            target = entry.resolve(strict=False)
            if target.parent != root:
                continue
            shutil.rmtree(target)
            removed += 1
        except OSError:
            continue
    return removed


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
        return [sys.executable, MEDIA_WORKER_MODE, str(directory)]
    return [sys.executable, "-m", "osia.media_reader", "--worker", str(directory)]


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


def _timeout_for(family: str) -> float:
    return {
        "image": IMAGE_TIMEOUT_SECONDS,
        "audio": AUDIO_TIMEOUT_SECONDS,
        "video": VIDEO_TIMEOUT_SECONDS,
    }.get(family, IMAGE_TIMEOUT_SECONDS)


def extract_media(
    data: bytes,
    identity: DocumentIdentity,
    language: str = "",
    *,
    include_private: bool = False,
) -> dict | tuple[dict, dict]:
    """Extract one already-identified media source in an isolated process."""

    value = bytes(data)
    if not value:
        raise MediaReadError("corrupted_media")
    if len(value) > MAX_FILE_BYTES:
        raise MediaReadError("resource_limit_exceeded", limit_kind="file_size")
    if identity.family not in {"image", "audio", "video"}:
        raise MediaReadError("unsupported_media")
    cleanup_orphan_jobs()
    directory = _job_directory()
    process: subprocess.Popen | None = None
    try:
        _private_write(directory / "input.bin", value)
        request = {
            "schema_version": 1,
            "format_label": identity.format_label,
            "family": identity.family,
            "language": str(language or "")[:24],
            "warnings": [
                code for code in identity.warnings
                if _SAFE_CODE_RE.fullmatch(str(code or ""))
            ][:8],
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
        deadline = time.monotonic() + _timeout_for(identity.family)
        memory_process = None
        try:
            import psutil

            memory_process = psutil.Process(process.pid)
        except Exception:
            pass
        while process.poll() is None:
            if time.monotonic() >= deadline:
                _stop_worker(process)
                raise MediaReadError(
                    "resource_limit_exceeded", limit_kind="execution_time",
                )
            if memory_process is not None:
                try:
                    resident = int(memory_process.memory_info().rss)
                    resident += sum(
                        int(child.memory_info().rss)
                        for child in memory_process.children(recursive=True)
                    )
                    if resident > MAX_WORKER_MEMORY_BYTES:
                        _stop_worker(process)
                        raise MediaReadError(
                            "resource_limit_exceeded", limit_kind="worker_memory",
                        )
                except MediaReadError:
                    raise
                except Exception:
                    memory_process = None
            time.sleep(0.02)
        if int(process.returncode or 0) != 0:
            raise MediaReadError("extraction_failure")
        raw = _bounded_read(directory / "result.json", MAX_WORKER_OUTPUT_BYTES)
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MediaReadError("extraction_failure") from exc
        if not isinstance(result, dict):
            raise MediaReadError("extraction_failure")
        if result.get("ok") is not True:
            category = str(result.get("category") or "extraction_failure")
            if category not in {
                "unsupported_media", "unsupported_codec", "corrupted_media",
                "encrypted_media", "drm_protected", "resource_limit_exceeded",
                "file_identity_mismatch", "extraction_failure",
            }:
                category = "extraction_failure"
            raise MediaReadError(
                category,
                limit_kind=str(result.get("limit_kind") or "") or None,
                stage=str(result.get("stage") or "") or None,
            )
        document = result.get("document")
        if not isinstance(document, dict) or document.get("schema_version") != 2:
            raise MediaReadError("extraction_failure")
        if (
            document.get("format_label") != identity.format_label
            or document.get("family") != identity.family
            or document.get("status") != "complete"
        ):
            raise MediaReadError("extraction_failure")
        private_metadata = result.get("private_metadata")
        if not isinstance(private_metadata, dict):
            private_metadata = {}
        return (document, private_metadata) if include_private else document
    finally:
        if process is not None and process.poll() is None:
            _stop_worker(process)
        _safe_remove_job(directory)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _chunks(records: list[tuple[str, dict]]) -> list[dict]:
    chunks = []
    text = ""
    provenance: list[dict] = []
    for raw, origin in records:
        remaining = str(raw or "").replace("\x00", "").strip()
        while remaining:
            separator = "\n" if text else ""
            capacity = CHUNK_CHARS - len(text) - len(separator)
            if capacity <= 0:
                chunks.append({
                    "chunk_id": f"chunk_{len(chunks) + 1:06d}",
                    "ordinal": len(chunks),
                    "text": text,
                    "provenance": list(provenance),
                })
                text = ""
                provenance = []
                continue
            piece = remaining[:capacity]
            text += separator + piece
            if origin and origin not in provenance and len(provenance) < 16:
                provenance.append(dict(origin))
            remaining = remaining[len(piece):]
    if text:
        chunks.append({
            "chunk_id": f"chunk_{len(chunks) + 1:06d}",
            "ordinal": len(chunks),
            "text": text,
            "provenance": list(provenance),
        })
    return chunks


def _base_document(format_label: str, family: str, warnings: list[str]) -> dict:
    return {
        "schema_version": 2,
        "format_label": format_label,
        "family": family,
        "status": "complete",
        "warnings": sorted(set(warnings))[:32],
        "metadata": {},
        "chunks": [],
        "sections": [],
        "headings": [],
        "paragraphs": [],
        "lists": [],
        "tables": [],
        "sheets": [],
        "slides": [],
        "notes": [],
        "links": [],
        "embedded": [],
        "provenance": [],
        "frames": [],
        "ocr_blocks": [],
        "tracks": [],
        "chapters": [],
        "transcript": [],
        "timeline": [],
    }


def _private_tags(value: Any, *, maximum_items: int = 12) -> dict:
    """Bound arbitrary decoder metadata for private storage only."""

    if not hasattr(value, "items"):
        return {}
    output = {}
    for key, item in list(value.items())[:maximum_items]:
        name = str(key)[:128]
        if isinstance(item, bytes):
            clean: Any = item[:128].hex()
        elif isinstance(item, (str, int, float, bool)) or item is None:
            clean = item if not isinstance(item, str) else item[:256]
        elif isinstance(item, (tuple, list)):
            clean = [str(part)[:64] for part in item[:16]]
        else:
            clean = str(item)[:256]
        output[name] = clean
    return output


def _ocr(image) -> tuple[str, float | None]:
    try:
        import numpy as np
        from osia.document_reader import _ocr_engine

        result = _ocr_engine()(np.asarray(image.convert("RGB")))
        texts = [str(item).strip() for item in (result.txts or ()) if str(item).strip()]
        scores = [
            float(item) for item in (result.scores or ())
            if isinstance(item, (int, float)) and math.isfinite(float(item))
        ]
        confidence = sum(scores) / len(scores) if scores else None
        return "\n".join(texts), round(confidence, 4) if confidence is not None else None
    except Exception as exc:
        raise MediaReadError("extraction_failure") from exc


def _selected_indices(total: int, maximum: int) -> list[int]:
    if total <= maximum:
        return list(range(total))
    if maximum <= 1:
        return [0]
    return sorted({
        round(index * (total - 1) / (maximum - 1))
        for index in range(maximum)
    })


def _extract_image(path: Path, request: dict) -> dict:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError

        with Image.open(path) as source:
            total = int(getattr(source, "n_frames", 1) or 1)
            if total <= 0:
                raise MediaReadError("corrupted_media")
            selected = _selected_indices(total, MAX_IMAGE_FRAMES)
            document = _base_document(
                str(request["format_label"]), "image", list(request.get("warnings") or []),
            )
            if total > len(selected):
                document["warnings"].append("representative_frames_only")
            cumulative = 0
            records = []
            durations = []
            first_width = first_height = 0
            for ordinal, source_index in enumerate(selected):
                source.seek(source_index)
                frame = ImageOps.exif_transpose(source.copy())
                width, height = map(int, frame.size)
                if width <= 0 or height <= 0:
                    raise MediaReadError("corrupted_media")
                if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                    raise MediaReadError(
                        "resource_limit_exceeded", limit_kind="image_dimensions",
                    )
                pixels = width * height
                if pixels > MAX_IMAGE_PIXELS:
                    raise MediaReadError(
                        "resource_limit_exceeded", limit_kind="image_pixels",
                    )
                cumulative += pixels
                if cumulative > MAX_IMAGE_CUMULATIVE_PIXELS:
                    raise MediaReadError(
                        "resource_limit_exceeded", limit_kind="cumulative_image_pixels",
                    )
                if not first_width:
                    first_width, first_height = width, height
                normalized = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
                frame_id = f"frame_{ordinal + 1:06d}"
                kind = "page" if request["format_label"] == "tiff" else "frame"
                duration_ms = max(0, min(int(source.info.get("duration", 0) or 0), 3_600_000))
                durations.append(duration_ms)
                document["frames"].append({
                    "frame_id": frame_id,
                    "ordinal": ordinal,
                    "source_ordinal": source_index,
                    "kind": kind,
                    "width": width,
                    "height": height,
                    "duration_ms": duration_ms,
                    "color_mode": normalized.mode,
                })
                text, confidence = _ocr(normalized)
                if text:
                    block = {
                        "ocr_block_id": f"ocr_{len(document['ocr_blocks']) + 1:06d}",
                        "frame_id": frame_id,
                        "text": text,
                        "confidence": confidence,
                        "provenance": {"frame_id": frame_id},
                    }
                    document["ocr_blocks"].append(block)
                    records.append((text, {"frame_id": frame_id}))
            confidences = [
                float(item["confidence"])
                for item in document["ocr_blocks"]
                if item.get("confidence") is not None
            ]
            document["metadata"] = {
                "width": first_width,
                "height": first_height,
                "page_count": total,
                "page_methods": ["ocr"] * len(selected),
                "confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
                "frame_count": total,
                "selected_frame_count": len(selected),
                "cumulative_decoded_pixels": cumulative,
                "orientation_corrected": True,
                "frame_durations_ms": durations,
            }
            document["chunks"] = _chunks(records)
            document["supported_queries"] = [
                "summary", "metadata", "ocr", "search", "chunks",
                "pages" if request["format_label"] == "tiff" else "frames",
                "provenance",
            ]
            document["_private_metadata"] = {
                "image_info": _private_tags(source.info),
                "exif": _private_tags(source.getexif()),
            }
            return document
    except MediaReadError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise MediaReadError("corrupted_media") from exc
    except Exception as exc:
        raise MediaReadError("extraction_failure") from exc


def _duration_seconds(container) -> float:
    if container.duration is not None:
        return max(0.0, float(container.duration) / 1_000_000.0)
    values = []
    for stream in container.streams:
        if stream.duration is not None and stream.time_base is not None:
            values.append(float(stream.duration * stream.time_base))
    return max(values, default=0.0)


def _safe_codec(stream) -> str:
    codec = str(getattr(stream.codec_context, "name", "") or "").casefold()
    codec = {"mp3float": "mp3", "aac_fixed": "aac"}.get(codec, codec)
    if not codec or codec in {"none", "unknown"}:
        raise MediaReadError("unsupported_codec")
    return codec


def _chapters(container) -> list[dict]:
    output = []
    raw_chapters = container.chapters() if callable(container.chapters) else container.chapters
    for ordinal, chapter in enumerate(list(raw_chapters or [])[:MAX_CHAPTERS]):
        base = _safe_float(getattr(chapter, "time_base", 0))
        output.append({
            "chapter_id": f"chapter_{ordinal + 1:06d}",
            "ordinal": ordinal,
            "start_seconds": round(_safe_float(chapter.start) * base, 3),
            "end_seconds": round(_safe_float(chapter.end) * base, 3),
            "title": str((chapter.metadata or {}).get("title") or "")[:256],
        })
    return output


def _track_inventory(container, family: str) -> tuple[list[dict], list[Any]]:
    maximum = MAX_AUDIO_TRACKS if family == "audio" else MAX_VIDEO_TRACKS
    streams = [stream for stream in container.streams if stream.type in {"audio", "video"}]
    if len(streams) > maximum:
        raise MediaReadError("resource_limit_exceeded", limit_kind="track_count")
    tracks = []
    for ordinal, stream in enumerate(streams):
        codec = _safe_codec(stream)
        allowed = _AUDIO_CODECS if stream.type == "audio" else _VIDEO_CODECS
        if codec not in allowed:
            raise MediaReadError("unsupported_codec")
        context = stream.codec_context
        item = {
            "track_id": f"track_{ordinal + 1:06d}",
            "ordinal": ordinal,
            "stream_index": int(stream.index),
            "kind": stream.type,
            "codec": codec,
            "duration_seconds": round(
                _safe_float(stream.duration) * _safe_float(stream.time_base), 3,
            ),
        }
        if stream.type == "audio":
            item.update({
                "channels": int(getattr(context, "channels", 0) or 0),
                "sample_rate": int(getattr(context, "sample_rate", 0) or 0),
            })
        else:
            item.update({
                "width": int(getattr(context, "width", 0) or 0),
                "height": int(getattr(context, "height", 0) or 0),
                "frame_rate": round(_safe_float(stream.average_rate), 3),
            })
        tracks.append(item)
    return tracks, streams


def _silence_regions(path: Path, stream_index: int) -> list[dict]:
    """Find bounded >=0.4 s silence runs without retaining decoded audio."""

    import av
    import numpy as np

    regions = []
    active_start = None
    current_end = 0.0
    with av.open(str(path), mode="r") as container:
        stream = next(
            (item for item in container.streams if int(item.index) == stream_index),
            None,
        )
        if stream is None:
            raise MediaReadError("extraction_failure")
        typed_index = list(container.streams.audio).index(stream)
        for frame in container.decode(audio=typed_index):
            raw = frame.to_ndarray()
            if raw.size == 0:
                continue
            if np.issubdtype(raw.dtype, np.integer):
                scale = float(max(abs(np.iinfo(raw.dtype).min), np.iinfo(raw.dtype).max))
                array = raw.astype("float32") / max(1.0, scale)
            else:
                array = raw.astype("float32", copy=False)
            peak = float(np.max(np.abs(array)))
            start = _safe_float(frame.pts) * _safe_float(frame.time_base)
            duration = _safe_float(frame.samples) / max(1, int(frame.sample_rate or 1))
            current_end = start + duration
            if peak < 0.01:
                if active_start is None:
                    active_start = start
            elif active_start is not None:
                if start - active_start >= 0.4 and len(regions) < 256:
                    regions.append({
                        "start_seconds": round(active_start, 3),
                        "end_seconds": round(start, 3),
                    })
                active_start = None
        if active_start is not None and current_end - active_start >= 0.4 and len(regions) < 256:
            regions.append({
                "start_seconds": round(active_start, 3),
                "end_seconds": round(current_end, 3),
            })
    return regions


def _transcribe(path: Path, language: str, track_id: str) -> tuple[list[dict], dict]:
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise MediaReadError(
            "extraction_failure", stage="transcription_import",
        ) from exc

    bundled = bundled_model_path("faster-whisper-small")
    model_ref = str(bundled) if bundled is not None else "small"
    try:
        model = WhisperModel(
            model_ref,
            device="cpu",
            compute_type="int8",
            local_files_only=True,
            cpu_threads=2,
            num_workers=1,
        )
    except Exception as exc:
        raise MediaReadError(
            "extraction_failure", stage="transcription_model_load",
        ) from exc
    try:
        normalized_language = str(language or "").strip().casefold()
        segments, info = model.transcribe(
            str(path),
            beam_size=1,
            language=None if normalized_language in {"", "auto"} else language,
            vad_filter=True,
        )
        output = []
        total_chars = 0
        for segment in segments:
            text = str(segment.text or "").strip()
            if not text:
                continue
            total_chars += len(text)
            if total_chars > MAX_TRANSCRIPT_CHARS or len(output) >= MAX_TRANSCRIPT_SEGMENTS:
                raise MediaReadError(
                    "resource_limit_exceeded", limit_kind="transcript_size",
                )
            probability = math.exp(min(0.0, _safe_float(segment.avg_logprob, -100.0)))
            output.append({
                "segment_id": f"segment_{len(output) + 1:06d}",
                "track_id": track_id,
                "ordinal": len(output),
                "start_seconds": round(_safe_float(segment.start), 3),
                "end_seconds": round(_safe_float(segment.end), 3),
                "text": text,
                "confidence": round(max(0.0, min(1.0, probability)), 4),
            })
        return output, {
            "speech_language": str(getattr(info, "language", "") or "")[:24],
            "language_confidence": round(
                max(0.0, min(1.0, _safe_float(getattr(info, "language_probability", 0)))), 4,
            ),
        }
    except MediaReadError:
        raise
    except Exception as exc:
        # No model/download/cloud fallback: extraction is not reported complete
        # when the one local transcription attempt cannot be produced.
        raise MediaReadError(
            "extraction_failure", stage="transcription_decode",
        ) from exc


def _extract_audio_track_wav(path: Path, stream_index: int, output: Path) -> None:
    """Decode one selected video audio track to a bounded local Whisper input."""

    import av

    written = 0
    frames_written = 0
    try:
        with _open_av(path) as container, wave.open(str(output), "wb") as writer:
            stream = next(
                (
                    item for item in container.streams
                    if int(item.index) == int(stream_index)
                ),
                None,
            )
            if stream is None or stream.type != "audio":
                raise MediaReadError("extraction_failure")
            typed_index = list(container.streams.audio).index(stream)
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=16_000,
            )
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)

            def _write_frames(converted) -> None:
                nonlocal written, frames_written
                values = converted if isinstance(converted, list) else [converted]
                for frame in values:
                    if frame is None:
                        continue
                    payload = frame.to_ndarray().astype("<i2", copy=False).tobytes()
                    written += len(payload)
                    if written > MAX_TRANSCRIPTION_WAV_BYTES:
                        raise MediaReadError(
                            "resource_limit_exceeded", limit_kind="temporary_audio",
                        )
                    writer.writeframesraw(payload)
                    frames_written += 1

            for frame in container.decode(audio=typed_index):
                _write_frames(resampler.resample(frame))
            _write_frames(resampler.resample(None))
    except MediaReadError:
        raise
    except Exception as exc:
        raise MediaReadError("extraction_failure") from exc
    if frames_written == 0 or written == 0:
        raise MediaReadError("extraction_failure")


def _open_av(path: Path):
    try:
        import av

        return av.open(str(path), mode="r")
    except Exception as exc:
        raise MediaReadError("corrupted_media") from exc


def _validate_container_format(container, expected: str) -> None:
    names = {item.strip().casefold() for item in str(container.format.name or "").split(",")}
    accepted = {
        "wav": {"wav"}, "mp3": {"mp3"}, "flac": {"flac"},
        "ogg": {"ogg"}, "m4a": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        "aac": {"aac"},
        "mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        "mov": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        "webm": {"matroska", "webm"}, "mkv": {"matroska", "webm"},
    }[expected]
    if not names.intersection(accepted):
        raise MediaReadError("file_identity_mismatch")


def _extract_audio(path: Path, request: dict) -> dict:
    with _open_av(path) as container:
        _validate_container_format(container, str(request["format_label"]))
        duration = _duration_seconds(container)
        if duration <= 0:
            raise MediaReadError("corrupted_media")
        if duration > MAX_AUDIO_SECONDS:
            raise MediaReadError("resource_limit_exceeded", limit_kind="duration")
        tracks, streams = _track_inventory(container, "audio")
        audio_pairs = [
            (track, stream) for track, stream in zip(tracks, streams)
            if track["kind"] == "audio"
        ]
        if not audio_pairs or any(track["kind"] == "video" for track in tracks):
            raise MediaReadError("file_identity_mismatch")
        for track, _stream in audio_pairs:
            if not (1 <= track["channels"] <= MAX_AUDIO_CHANNELS):
                raise MediaReadError("resource_limit_exceeded", limit_kind="channels")
            if not (1 <= track["sample_rate"] <= MAX_AUDIO_SAMPLE_RATE):
                raise MediaReadError("resource_limit_exceeded", limit_kind="sample_rate")
        chapters = _chapters(container)
        original_metadata = {
            "container": _private_tags(container.metadata),
            "streams": [
                _private_tags(stream.metadata) for stream in streams
            ],
        }
        raw_chapters = container.chapters() if callable(container.chapters) else container.chapters
        if len(list(raw_chapters or [])) > MAX_CHAPTERS:
            raise MediaReadError("resource_limit_exceeded", limit_kind="chapter_count")
    selected_track, _selected_stream = audio_pairs[0]
    silence = _silence_regions(path, int(selected_track["stream_index"]))
    transcript, speech = _transcribe(
        path, str(request.get("language") or ""), selected_track["track_id"],
    )
    document = _base_document(
        str(request["format_label"]), "audio", list(request.get("warnings") or []),
    )
    document["tracks"] = tracks
    document["chapters"] = chapters
    document["transcript"] = transcript
    document["metadata"] = {
        "duration_seconds": round(duration, 3),
        "track_count": len(tracks),
        "chapter_count": len(chapters),
        "selected_track_id": selected_track["track_id"],
        "codec": selected_track["codec"],
        "channels": selected_track["channels"],
        "sample_rate": selected_track["sample_rate"],
        "silence_regions": silence,
        **speech,
    }
    document["chunks"] = _chunks([
        (item["text"], {"segment_id": item["segment_id"], "track_id": item["track_id"]})
        for item in transcript
    ])
    document["supported_queries"] = [
        "summary", "metadata", "search", "chunks", "transcript",
        "time_range", "tracks", "chapters", "provenance",
    ]
    document["_private_metadata"] = original_metadata
    return document


def _representative_times(path: Path, stream_index: int, duration: float) -> list[float]:
    import av

    interval_count = min(12, max(1 if duration <= 1.0 else 3, math.ceil(duration / 5.0) + 1))
    targets = {
        round(index * max(0.0, duration - 0.001) / max(1, interval_count - 1), 3)
        for index in range(interval_count)
    }
    # Packet/keyframe timing adds bounded scene candidates without decoding
    # every frame. Large keyframe-size changes are a deterministic scene proxy.
    with av.open(str(path), mode="r") as container:
        stream = next(
            (item for item in container.streams if int(item.index) == stream_index),
            None,
        )
        if stream is None:
            raise MediaReadError("extraction_failure")
        previous_size = 0
        for packet in container.demux(stream):
            if not packet.is_keyframe or packet.pts is None:
                continue
            size = int(packet.size or 0)
            if previous_size and (size > previous_size * 1.8 or size * 1.8 < previous_size):
                targets.add(round(float(packet.pts * stream.time_base), 3))
                if len(targets) >= MAX_VIDEO_FRAMES:
                    break
            previous_size = size
    return sorted(value for value in targets if 0 <= value <= duration)[:MAX_VIDEO_FRAMES]


def _decode_frame_at(path: Path, stream_index: int, seconds: float):
    import av

    with av.open(str(path), mode="r") as container:
        stream = next(
            (item for item in container.streams if int(item.index) == stream_index),
            None,
        )
        if stream is None:
            raise MediaReadError("extraction_failure")
        target = int(seconds / float(stream.time_base)) if stream.time_base else 0
        container.seek(max(0, target), stream=stream, backward=True, any_frame=False)
        fallback = None
        typed_index = list(container.streams.video).index(stream)
        for frame in container.decode(video=typed_index):
            timestamp = _safe_float(frame.pts) * _safe_float(frame.time_base)
            fallback = (frame.to_image(), timestamp)
            if timestamp + 0.05 >= seconds:
                return fallback
        if fallback is not None:
            return fallback
    raise MediaReadError("extraction_failure")


def _extract_video(path: Path, request: dict) -> dict:
    with _open_av(path) as container:
        _validate_container_format(container, str(request["format_label"]))
        duration = _duration_seconds(container)
        if duration <= 0:
            raise MediaReadError("corrupted_media")
        if duration > MAX_VIDEO_SECONDS:
            raise MediaReadError("resource_limit_exceeded", limit_kind="duration")
        tracks, streams = _track_inventory(container, "video")
        video_pairs = [
            (track, stream) for track, stream in zip(tracks, streams)
            if track["kind"] == "video"
        ]
        audio_pairs = [
            (track, stream) for track, stream in zip(tracks, streams)
            if track["kind"] == "audio"
        ]
        if not video_pairs:
            raise MediaReadError("file_identity_mismatch")
        for track, _stream in video_pairs:
            if not (1 <= track["width"] <= MAX_VIDEO_WIDTH and 1 <= track["height"] <= MAX_VIDEO_HEIGHT):
                raise MediaReadError("resource_limit_exceeded", limit_kind="video_dimensions")
        chapters = _chapters(container)
        original_metadata = {
            "container": _private_tags(container.metadata),
            "streams": [
                _private_tags(stream.metadata) for stream in streams
            ],
        }
        raw_chapters = container.chapters() if callable(container.chapters) else container.chapters
        if len(list(raw_chapters or [])) > MAX_CHAPTERS:
            raise MediaReadError("resource_limit_exceeded", limit_kind="chapter_count")
    selected_video, _video_stream = video_pairs[0]
    video_duration = _safe_float(selected_video.get("duration_seconds"), duration) or duration
    video_stream_index = int(selected_video["stream_index"])
    try:
        times = _representative_times(path, video_stream_index, video_duration)
    except MediaReadError as exc:
        raise MediaReadError(
            exc.category, limit_kind=exc.limit_kind, stage="frame_sampling",
        ) from exc
    transcript = []
    speech = {"speech_language": "", "language_confidence": 0.0}
    selected_audio_id = ""
    if audio_pairs:
        # Keep the two local model families memory-sequential. RapidOCR is a
        # process singleton, so loading it first and then Whisper can briefly
        # cross the worker's 512 MiB cap even though each phase is bounded.
        # Whisper is local to _transcribe and is released before OCR starts.
        selected_audio = audio_pairs[0][0]
        selected_audio_id = selected_audio["track_id"]
        transcription_input = path.with_name("selected-audio.wav")
        try:
            try:
                _extract_audio_track_wav(
                    path, int(selected_audio["stream_index"]), transcription_input,
                )
            except MediaReadError as exc:
                raise MediaReadError(
                    exc.category, limit_kind=exc.limit_kind,
                    stage="audio_track_extraction",
                ) from exc
            try:
                transcript, speech = _transcribe(
                    transcription_input,
                    str(request.get("language") or ""),
                    selected_audio_id,
                )
            except MediaReadError as exc:
                raise MediaReadError(
                    exc.category, limit_kind=exc.limit_kind,
                    stage=exc.stage or "transcription",
                ) from exc
        finally:
            try:
                transcription_input.unlink(missing_ok=True)
            except OSError:
                pass
    document = _base_document(
        str(request["format_label"]), "video", list(request.get("warnings") or []),
    )
    cumulative = 0
    records = []
    for ordinal, target in enumerate(times):
        try:
            image, actual = _decode_frame_at(path, video_stream_index, target)
        except MediaReadError as exc:
            raise MediaReadError(
                exc.category, limit_kind=exc.limit_kind, stage="frame_decode",
            ) from exc
        width, height = image.size
        pixels = int(width) * int(height)
        if cumulative + pixels > MAX_VIDEO_FRAME_PIXELS:
            document["warnings"].append("representative_frame_limit")
            break
        cumulative += pixels
        frame_id = f"frame_{ordinal + 1:06d}"
        document["frames"].append({
            "frame_id": frame_id,
            "ordinal": ordinal,
            "kind": "frame",
            "time_seconds": round(actual, 3),
            "width": int(width),
            "height": int(height),
        })
        try:
            text, confidence = _ocr(image.convert("RGB"))
        except MediaReadError as exc:
            raise MediaReadError(
                exc.category, limit_kind=exc.limit_kind, stage="ocr",
            ) from exc
        if text:
            document["ocr_blocks"].append({
                "ocr_block_id": f"ocr_{len(document['ocr_blocks']) + 1:06d}",
                "frame_id": frame_id,
                "time_seconds": round(actual, 3),
                "text": text,
                "confidence": confidence,
                "provenance": {"frame_id": frame_id},
            })
            records.append((text, {"frame_id": frame_id}))
    if transcript:
        records.extend((item["text"], {"segment_id": item["segment_id"]}) for item in transcript)
    document["tracks"] = tracks
    document["chapters"] = chapters
    document["transcript"] = transcript
    document["metadata"] = {
        "duration_seconds": round(duration, 3),
        "width": selected_video["width"],
        "height": selected_video["height"],
        "frame_rate": selected_video["frame_rate"],
        "track_count": len(tracks),
        "chapter_count": len(chapters),
        "selected_video_track_id": selected_video["track_id"],
        "selected_audio_track_id": selected_audio_id,
        "representative_frame_count": len(document["frames"]),
        "cumulative_frame_pixels": cumulative,
        **speech,
    }
    timeline = []
    for item in document["frames"]:
        timeline.append({
            "timeline_id": f"timeline_{len(timeline) + 1:06d}",
            "kind": "frame",
            "time_seconds": item["time_seconds"],
            "start_seconds": item["time_seconds"],
            "end_seconds": item["time_seconds"],
            "frame_id": item["frame_id"],
        })
    for item in document["ocr_blocks"]:
        timeline.append({
            "timeline_id": f"timeline_{len(timeline) + 1:06d}",
            "kind": "ocr",
            "time_seconds": item["time_seconds"],
            "start_seconds": item["time_seconds"],
            "end_seconds": item["time_seconds"],
            "frame_id": item["frame_id"],
            "text": item["text"],
        })
    for item in transcript:
        timeline.append({
            "timeline_id": f"timeline_{len(timeline) + 1:06d}",
            "kind": "transcript",
            "start_seconds": item["start_seconds"],
            "end_seconds": item["end_seconds"],
            "segment_id": item["segment_id"],
            "track_id": item["track_id"],
            "text": item["text"],
        })
    document["timeline"] = sorted(
        timeline,
        key=lambda item: (
            _safe_float(item.get("start_seconds")), str(item.get("kind")),
            str(item.get("timeline_id")),
        ),
    )
    document["chunks"] = _chunks(records)
    document["supported_queries"] = [
        "summary", "metadata", "search", "chunks", "frames", "ocr",
        "transcript", "time_range", "timeline", "tracks", "chapters",
        "provenance",
    ]
    document["_private_metadata"] = original_metadata
    return document


def _worker_extract(directory: Path, request: dict) -> dict:
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise MediaReadError("extraction_failure")
    path = directory / "input.bin"
    data = _bounded_read(path, MAX_FILE_BYTES)
    try:
        identity = identify_document(data)
    except Exception as exc:
        category = str(getattr(exc, "category", "corrupted_media"))
        if category in {"corrupted_document", "unsupported_format"}:
            category = "corrupted_media"
        raise MediaReadError(category, limit_kind=getattr(exc, "limit_kind", None)) from exc
    if (
        identity.format_label != request.get("format_label")
        or identity.family != request.get("family")
    ):
        raise MediaReadError("file_identity_mismatch")
    if identity.family == "image" and identity.format_label in IMAGE_FORMATS:
        return _extract_image(path, request)
    if identity.family == "audio" and identity.format_label in AUDIO_FORMATS:
        return _extract_audio(path, request)
    if identity.family == "video" and identity.format_label in VIDEO_FORMATS:
        return _extract_video(path, request)
    raise MediaReadError("unsupported_media")


def _worker_result(directory: Path, result: dict) -> None:
    encoded = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_WORKER_OUTPUT_BYTES:
        raise MediaReadError("resource_limit_exceeded", limit_kind="output_size")
    _private_write(directory / "result.json", encoded)


def worker_main(job_path: str) -> int:
    directory: Path | None = None
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
        if directory.parent != root or not WORKER_JOB_RE.fullmatch(directory.name):
            return 2
        request = json.loads(
            _bounded_read(directory / "request.json", 16 * 1024).decode("utf-8"),
        )
        document = _worker_extract(directory, request)
        private_metadata = document.pop("_private_metadata", {})
        _worker_result(directory, {
            "ok": True,
            "document": document,
            "private_metadata": private_metadata,
        })
        return 0
    except MediaReadError as exc:
        if directory is None:
            return 1
        try:
            result = {"ok": False, "category": exc.category}
            if exc.limit_kind and _SAFE_CODE_RE.fullmatch(exc.limit_kind):
                result["limit_kind"] = exc.limit_kind
            if exc.stage and _SAFE_CODE_RE.fullmatch(exc.stage):
                result["stage"] = exc.stage
            _worker_result(directory, result)
            return 0
        except Exception:
            return 1
    except Exception:
        if directory is None:
            return 1
        try:
            _worker_result(directory, {"ok": False, "category": "extraction_failure"})
            return 0
        except Exception:
            return 1


def verify_packaged_dependencies() -> bool:
    """Offline import/model probe used by the frozen package smoke."""

    try:
        os.environ["HF_HUB_OFFLINE"] = "1"
        import av  # noqa: F401
        import soundfile  # noqa: F401
        from PIL import Image  # noqa: F401
        from faster_whisper import WhisperModel
        from osia.document_reader import _ocr_engine

        model = bundled_model_path("faster-whisper-small")
        if is_frozen() and model is None:
            return False
        if model is not None:
            WhisperModel(str(model), device="cpu", compute_type="int8", local_files_only=True)
        _ocr_engine()
        return True
    except Exception:
        return False


def run_packaged_media_smoke(root: Path) -> bool:
    """Exercise frozen image/audio/video workers with synthetic local media."""

    try:
        import io
        import wave

        import av
        import numpy as np
        from PIL import Image

        root = Path(root).resolve(strict=True)

        image_buffer = io.BytesIO()
        Image.new("RGB", (64, 32), "white").save(image_buffer, format="PNG")
        image_data = image_buffer.getvalue()
        image = extract_media(
            image_data, identify_document(image_data, display_name="smoke.png"),
        )
        if image.get("family") != "image" or len(image.get("frames") or []) != 1:
            return False

        audio_buffer = io.BytesIO()
        with wave.open(audio_buffer, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16_000)
            writer.writeframes(b"\x00\x00" * 8_000)
        audio_data = audio_buffer.getvalue()
        audio = extract_media(
            audio_data, identify_document(audio_data, display_name="smoke.wav"),
        )
        if audio.get("family") != "audio" or not audio.get("tracks"):
            return False

        video_path = root / "media-smoke.mp4"
        container = av.open(str(video_path), mode="w")
        stream = container.add_stream("libx264", rate=2)
        stream.width = 160
        stream.height = 90
        stream.pix_fmt = "yuv420p"
        for index in range(4):
            pixels = np.zeros((90, 160, 3), dtype=np.uint8)
            pixels[:, :, index % 3] = 80 + index * 30
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        video_data = video_path.read_bytes()
        video = extract_media(
            video_data, identify_document(video_data, display_name="smoke.mp4"),
        )
        return bool(video.get("family") == "video" and video.get("frames"))
    except Exception:
        return False


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2]))
    raise SystemExit(2)


__all__ = [
    "AUDIO_FORMATS", "IMAGE_FORMATS", "MediaReadError", "VIDEO_FORMATS",
    "cleanup_orphan_jobs", "extract_media", "run_packaged_media_smoke",
    "verify_packaged_dependencies", "worker_main",
]
