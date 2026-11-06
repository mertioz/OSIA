"""PC-owned screenshot attachments and bounded context for completed Chat turns."""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import uuid
from pathlib import Path

from osia.config import CONFIG_DIR


SCREENSHOTS_DIR = Path(CONFIG_DIR) / "screenshots"
MAX_PREVIEW_BYTES = 96 * 1024
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_FILE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_ID = re.compile(r"[a-f0-9]{32}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_OBSERVATION_LOCK = threading.Lock()
_OBSERVATIONS: dict[tuple[str, str], dict] = {}


def _turn_directory(conversation_id: str, turn_id: str) -> Path:
    if not conversation_id or not turn_id:
        raise ValueError("screenshot_turn_required")
    root = SCREENSHOTS_DIR.resolve()
    binding = json.dumps([conversation_id, turn_id], separators=(",", ":"))
    path = root / hashlib.sha256(binding.encode("utf-8")).hexdigest()[:32]
    if path.resolve() != path:
        raise ValueError("screenshot_directory_not_owned")
    return path


def normalize_screenshots(value: object) -> list[dict]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        screenshot_id, mime = item.get("id"), item.get("mime")
        digest = item.get("sha256")
        width, height, size = item.get("width"), item.get("height"), item.get("byte_size")
        if (
            not isinstance(screenshot_id, str) or not _ID.fullmatch(screenshot_id)
            or not isinstance(mime, str) or mime not in _FILE_TYPES
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or type(width) is not int or type(height) is not int
            or not 0 < width <= 32_768 or not 0 < height <= 32_768
            or type(size) is not int or not 0 < size <= _MAX_IMAGE_BYTES
        ):
            continue
        result.append({
            "id": screenshot_id, "mime": mime, "width": width, "height": height,
            "byte_size": size, "sha256": digest,
        })
    return result


def message_screenshots(conversation: dict | None, turn_id: str, message_id: str = "") -> list[dict]:
    if not conversation or not turn_id:
        return []
    for message in reversed(conversation.get("messages") or []):
        if (isinstance(message, dict) and message.get("role") == "assistant"
                and message.get("_turn_id") == turn_id
                and (not message_id or message.get("_message_id") == message_id)):
            return normalize_screenshots(message.get("_screenshots"))
    return []


def save_chat_screenshot(conversation_id: str, turn_id: str, artifact) -> dict:
    """Save a validated capture under the exact turn, with no model-chosen path."""
    screenshot_id = uuid.uuid4().hex
    records = normalize_screenshots([{
        "id": screenshot_id, "mime": artifact.mime_type,
        "width": artifact.width, "height": artifact.height,
        "byte_size": len(artifact.image_bytes), "sha256": artifact.content_hash,
    }])
    if not records:
        raise ValueError("invalid_screenshot")
    record = records[0]
    directory = _turn_directory(conversation_id, turn_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    image = directory / f"capture-{screenshot_id}{_FILE_TYPES[record['mime']]}"
    index = directory / f"{screenshot_id}.json"
    staging = directory / f".{screenshot_id}.tmp"
    created = []
    try:
        with image.open("xb") as stream:
            created.append(image)
            stream.write(artifact.image_bytes)
        with staging.open("x", encoding="utf-8") as stream:
            created.append(staging)
            json.dump(record, stream)
        staging.replace(index)
    except OSError:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return record


def screenshots_for_turn(conversation_id: str, turn_id: str) -> list[dict]:
    try:
        paths = sorted(_turn_directory(conversation_id, turn_id).glob("*.json"),
                       key=lambda path: path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return []
    result = []
    for path in paths:
        try:
            if path.is_symlink() or path.stat().st_size > 2048:
                continue
            records = normalize_screenshots([json.loads(path.read_text("utf-8"))])
            if records and records[0]["id"] == path.stem:
                result.append(records[0])
        except (OSError, ValueError):
            continue
    return result


def screenshot_path(conversation_id: str, turn_id: str, screenshot: dict) -> Path:
    records = normalize_screenshots([screenshot])
    if not records:
        raise ValueError("invalid_screenshot")
    record = records[0]
    directory = _turn_directory(conversation_id, turn_id)
    path = directory / f"capture-{record['id']}{_FILE_TYPES[record['mime']]}"
    if path.resolve() != path or not path.is_file() or path.stat().st_size != record["byte_size"]:
        raise ValueError("screenshot_unavailable")
    return path


def read_chat_screenshot(conversation_id: str, turn_id: str, screenshot: dict) -> bytes:
    path = screenshot_path(conversation_id, turn_id, screenshot)
    with path.open("rb") as stream:
        data = stream.read(_MAX_IMAGE_BYTES + 1)
    if len(data) != screenshot["byte_size"] or hashlib.sha256(data).hexdigest() != screenshot["sha256"]:
        raise ValueError("screenshot_unavailable")
    return data


def screenshot_preview(conversation_id: str, turn_id: str, screenshot: dict) -> bytes:
    """Create a bounded mobile preview from the exact saved pixels."""
    return encode_screenshot_preview(read_chat_screenshot(conversation_id, turn_id, screenshot))


def encode_screenshot_preview(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
    image.thumbnail((1280, 960), Image.Resampling.LANCZOS)
    for _ in range(5):
        for quality in (85, 70, 55):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() <= MAX_PREVIEW_BYTES:
                return output.getvalue()
        image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                        Image.Resampling.LANCZOS)
    raise ValueError("screenshot_preview_too_large")


def remember_chat_observation(conversation_id: str, turn_id: str, reference: dict) -> None:
    """Retain one observed image per turn, separate from the live action frame."""
    if not conversation_id or not turn_id or not reference.get("image_id"):
        return
    from osia.image_inputs import (
        ImageInputError, create_image_artifact, reference_owner_id,
        register_image_artifact, release_image_owner, resolve_image_reference,
    )

    try:
        artifact = resolve_image_reference(reference, owner_id=reference_owner_id(
            reference, conversation_id=conversation_id, turn_id=turn_id,
        ))
        historical = create_image_artifact(
            artifact.image_bytes, artifact.mime_type, artifact.source_type,
            lifecycle="durable_backed", expires_at=time.time() + 24 * 60 * 60,
        )
        saved = register_image_artifact(historical, owner_id=conversation_id)
    except (ImageInputError, OSError, ValueError):
        return
    with _OBSERVATION_LOCK:
        key = (conversation_id, turn_id)
        previous = _OBSERVATIONS.pop(key, None)
        _OBSERVATIONS[key] = saved
        retired = [(conversation_id, previous)] if previous else []
        while len(_OBSERVATIONS) > 64:
            oldest = next(iter(_OBSERVATIONS))
            retired.append((oldest[0], _OBSERVATIONS.pop(oldest)))
    for owner_id, item in retired:
        if owner_id == conversation_id and item["image_id"] == saved["image_id"]:
            continue
        try:
            release_image_owner(item["image_id"], owner_id=owner_id)
        except (ImageInputError, OSError, ValueError):
            pass


def retain_chat_observation(conversation_id: str, turn_id: str) -> dict | None:
    """Move the exact turn's recorded observation into its durable chat history."""
    with _OBSERVATION_LOCK:
        return _OBSERVATIONS.pop((conversation_id, turn_id), None)
