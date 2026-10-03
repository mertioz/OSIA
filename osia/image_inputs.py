"""Canonical image artifacts plus provider-only multimodal serialization.

Images enter OSIA once as :class:`ImageArtifact` objects.  User uploads,
desktop captures, browser captures and bounded crops therefore share the same
validation, ephemeral lifecycle and provider transport.  Provider-specific
code in this module only serializes pixels; it never selects another model or
interprets image content.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from osia.config import CONFIG_DIR


_HISTORICAL_IMAGE_REFERENCE = re.compile(
    r"\b(?:(?:this|that)\s+(?:same\s+)?|the\s+same\s+|"
    r"(?:previous|earlier|prior)\s+)(?:image|photo|picture|screenshot)\b|"
    r"\b(?:image|photo|capture)\s+(?:precedente|précédente|d avant|d'avant)\b|"
    r"\b(?:cette|la)\s+(?:m[eê]me\s+)?(?:image|photo|capture)\b|"
    r"^(?:and|et|what about)\b.{0,120}\b(?:it|this|that|image|photo|picture|"
    r"screenshot|button|bottom|top|left|right|dessus|dessous|haut|bas)\b",
    re.IGNORECASE,
)


_ALL_OSIA_IMAGE_MIMES = frozenset({
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})

_ADAPTERS = {
    "anthropic": ("openai_image_url", _ALL_OSIA_IMAGE_MIMES),
    "chatgpt_account": ("app_server_local_image", _ALL_OSIA_IMAGE_MIMES),
    "deepseek": ("openai_image_url", _ALL_OSIA_IMAGE_MIMES),
    "google": (
        "openai_image_url",
        frozenset({"image/jpeg", "image/png", "image/webp"}),
    ),
    "groq": (
        "openai_image_url",
        frozenset({"image/jpeg", "image/png"}),
    ),
    "mistral": (
        "mistral_image_url",
        frozenset({"image/jpeg", "image/png"}),
    ),
    "openai": ("openai_image_url", _ALL_OSIA_IMAGE_MIMES),
    "xai": (
        "openai_image_url",
        frozenset({"image/jpeg", "image/png"}),
    ),
}

MAX_IMAGE_DIMENSION = 32_768
MAX_IMAGE_PIXELS = 64_000_000
MAX_EPHEMERAL_IMAGES = 64
MAX_EPHEMERAL_IMAGE_BYTES = 128 * 1024 * 1024
MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_NORMALIZED_IMAGE_BYTES = 20 * 1024 * 1024
MEDIA_STORE_MAX_FILES = 256
MEDIA_STORE_MAX_BYTES = 256 * 1024 * 1024
MEDIA_LEASE_SECONDS = 10 * 60
MEDIA_DELIVERY_RETENTION_SECONDS = 24 * 60 * 60
_SOURCE_TTL_SECONDS = {
    "user_upload": 24 * 60 * 60,
    "desktop_capture": 90,
    "browser_capture": 90,
    "refined_crop": 30,
}
_PROVIDER_WIRE_MAX_BYTES = {
    "mistral": 20_000_000,
    "xai": 20 * 1024 * 1024,
}


@dataclass(frozen=True)
class ProviderImagePolicy:
    """Canonical provider envelope bounds for every image entry point."""

    maximum_image_bytes: int | None = None
    maximum_encoded_payload_bytes: int | None = None
    maximum_images: int | None = None


_MB = 1000 * 1000
_MIB = 1024 * 1024
PROVIDER_IMAGE_POLICIES: dict[str, ProviderImagePolicy] = {
    "chatgpt_account": ProviderImagePolicy(maximum_image_bytes=20 * _MIB),
    "deepseek": ProviderImagePolicy(
        maximum_image_bytes=32 * _MIB,
        maximum_encoded_payload_bytes=48 * _MIB,
        maximum_images=600,
    ),
    "google": ProviderImagePolicy(maximum_encoded_payload_bytes=100 * _MB),
    "groq": ProviderImagePolicy(
        maximum_encoded_payload_bytes=20 * _MB,
        maximum_images=5,
    ),
    "mistral": ProviderImagePolicy(maximum_image_bytes=20 * _MB),
    "openai": ProviderImagePolicy(
        maximum_encoded_payload_bytes=512 * _MB,
        maximum_images=1500,
    ),
    "xai": ProviderImagePolicy(maximum_image_bytes=20 * _MIB),
}


class ImageInputError(RuntimeError):
    """A safe user-facing image-input rejection raised before inference."""

    def __init__(self, message: str, *, reason_code: str = "IMAGE_INPUT_UNAVAILABLE"):
        super().__init__(message)
        self.reason_code = str(reason_code or "IMAGE_INPUT_UNAVAILABLE")


@dataclass(frozen=True)
class ImageInputDecision:
    provider: str
    model: str
    model_known: bool
    model_supports_vision: bool
    adapter_supports_images: bool
    account_path_permits_images: bool
    formats_accepted: bool
    encoding: str | None
    usable: bool
    reason: str


@dataclass(frozen=True)
class DecodedImage:
    mime: str
    data: bytes
    width: int = 0
    height: int = 0
    content_hash: str = ""


class ImageSourceType(str, Enum):
    USER_UPLOAD = "user_upload"
    DESKTOP_CAPTURE = "desktop_capture"
    BROWSER_CAPTURE = "browser_capture"
    REFINED_CROP = "refined_crop"


@dataclass(frozen=True)
class ImageArtifact:
    """Provider-neutral, privacy-safe identity for one validated image."""

    image_id: str
    source_type: str
    mime_type: str
    width: int
    height: int
    content_hash: str
    image_bytes: bytes
    created_at: float
    parent_image_id: str = ""
    hwnd: str = ""
    surface_id: str = ""
    generation: str = ""
    application_identity: str = ""
    bounds: tuple[int, int, int, int] | None = None
    dpi: int = 0
    monitor: str = ""
    freshness: str = ""
    source_dimensions: tuple[int, int] | None = None
    provider_dimensions: tuple[int, int] | None = None
    detail_level: str = "auto"
    crop_transform: dict | None = None
    lifecycle: str = "ephemeral"
    expires_at: float = 0.0
    privacy_class: str = "private"
    metadata_stripped: bool = False
    capture_method: str = ""
    source_pixel_format: str = ""
    pixel_format_conversion: str = ""
    alpha_handling: str = ""
    source_color_space: str = ""
    color_space_conversion: str = ""
    capture_attempts: tuple[dict, ...] = ()
    fallback_used: bool = False

    def __repr__(self) -> str:
        return (
            "ImageArtifact("
            f"image_id={self.image_id!r}, source_type={self.source_type!r}, "
            f"mime_type={self.mime_type!r}, width={self.width}, "
            f"height={self.height}, content_hash={self.content_hash!r})"
        )

    def safe_reference(self) -> dict:
        reference = {
            "type": "image_ref",
            "image_id": self.image_id,
            "source_type": self.source_type,
            "mime": self.mime_type,
            "width": self.width,
            "height": self.height,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "detail": self.detail_level,
            "byte_size": len(self.image_bytes),
            "lifecycle": self.lifecycle,
            "expires_at": self.expires_at,
            "privacy": {
                "class": self.privacy_class,
                "metadata_stripped": self.metadata_stripped,
            },
        }
        if self.capture_method:
            reference.update({
                "capture_method": self.capture_method,
                "source_pixel_format": self.source_pixel_format,
                "pixel_format_conversion": self.pixel_format_conversion,
                "alpha_handling": self.alpha_handling,
                "source_color_space": self.source_color_space,
                "color_space_conversion": self.color_space_conversion,
                "capture_attempts": [
                    dict(item) for item in self.capture_attempts
                ],
                "fallback_used": self.fallback_used,
            })
        if self.parent_image_id:
            reference["parent_image_id"] = self.parent_image_id
        return reference

    def safe_metadata(self) -> dict:
        metadata = dict(self.safe_reference())
        metadata.pop("type", None)
        metadata.update({
            "source_dimensions": list(
                self.source_dimensions or (self.width, self.height)
            ),
            "provider_dimensions": list(
                self.provider_dimensions or (self.width, self.height)
            ),
        })
        if self.surface_id:
            metadata.update({
                "hwnd": self.hwnd,
                "surface_id": self.surface_id,
                "generation": self.generation,
                "application_identity": self.application_identity,
                "bounds": list(self.bounds) if self.bounds is not None else None,
                "dpi": self.dpi,
                "monitor": self.monitor,
                "freshness": self.freshness,
            })
        if self.crop_transform:
            metadata["crop_transform"] = dict(self.crop_transform)
        return metadata


_IMAGE_STORE_LOCK = threading.RLock()


@dataclass
class _StoredImage:
    artifact: ImageArtifact
    owner_hashes: frozenset[str]
    producer_pid: int
    producer_identity: str
    storage_name: str
    leases: dict[str, float] = field(default_factory=dict)
    owner_scope: str = "conversation"


_IMAGE_STORE: "OrderedDict[str, _StoredImage]" = OrderedDict()
_IMAGE_STORE_BYTES = 0


def _media_root() -> Path:
    return Path(CONFIG_DIR) / "private_media"


def _artifact_root() -> Path:
    return _media_root() / "artifacts"


def _delivery_root() -> Path:
    return _media_root() / "deliveries"


def _owner_hash(owner_id: str) -> str:
    value = str(owner_id or "")
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def turn_scoped_media_owner(conversation_id: str, turn_id: str) -> str:
    """Return one opaque-in-storage owner for ephemeral turn media."""

    conversation = str(conversation_id or "").strip()
    turn = str(turn_id or "").strip()
    if not conversation and not turn:
        # Compatibility callers that never cross a process boundary remain
        # process-local and unshareable. Production Desktop turns bind both.
        return _effective_owner_id("")
    if not conversation or not turn:
        raise ImageInputError(
            "ERROR: an ephemeral image has no exact turn owner; no provider "
            "request was sent.",
            reason_code="IMAGE_OWNER_MISMATCH",
        )
    return f"turn-media\0{conversation}\0{turn}"


def _effective_owner_id(owner_id: str) -> str:
    """Keep ownerless compatibility process-local, never globally shareable."""

    owner = str(owner_id or "").strip()
    return owner or f"process-media\0{_process_identity()}"


def _process_identity(pid: int | None = None) -> str:
    numeric_pid = int(os.getpid() if pid is None else pid)
    try:
        import psutil

        return f"{numeric_pid}:{psutil.Process(numeric_pid).create_time():.6f}"
    except Exception:
        return str(numeric_pid)


def _producer_is_alive(pid: object, identity: object) -> bool:
    try:
        numeric_pid = int(pid or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if numeric_pid == os.getpid():
        return str(identity or "") in {"", _process_identity()}
    try:
        import psutil

        process = psutil.Process(numeric_pid)
        return bool(
            process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and str(identity or "")
            == f"{numeric_pid}:{process.create_time():.6f}"
        )
    except Exception:
        return False


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ImageInputError(
            "ERROR: the private image store is unavailable; no provider request "
            "was sent.",
            reason_code="IMAGE_STORE_UNAVAILABLE",
        )
    try:
        path.chmod(0o700)
    except OSError:
        pass


@contextlib.contextmanager
def _media_process_lock():
    root = _media_root()
    _private_directory(root)
    lock_path = root / ".media.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes) -> None:
    _private_directory(path.parent)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_urlsafe(8)}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _artifact_metadata_path(image_id: str) -> Path:
    digest = hashlib.sha256(str(image_id or "").encode("utf-8")).hexdigest()
    return _artifact_root() / f"{digest}.json"


def _load_artifact_metadata(image_id: str) -> dict | None:
    path = _artifact_metadata_path(image_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise ImageInputError(
            "ERROR: an attached image reference is unavailable; no provider "
            "request was sent.",
            reason_code="IMAGE_REF_UNAVAILABLE",
        )
    if not isinstance(value, dict) or value.get("image_id") != image_id:
        raise ImageInputError(
            "ERROR: an attached image reference is unavailable; no provider "
            "request was sent.",
            reason_code="IMAGE_REF_UNAVAILABLE",
        )
    return value


def _active_leases(metadata: dict, *, now: float | None = None) -> dict[str, float]:
    moment = time.time() if now is None else float(now)
    raw = metadata.get("leases") if isinstance(metadata, dict) else {}
    return {
        str(key): float(value)
        for key, value in (raw.items() if isinstance(raw, dict) else ())
        if str(key) and isinstance(value, (int, float)) and float(value) > moment
    }


def _artifact_store_payload(
    stored: _StoredImage,
) -> dict:
    artifact = stored.artifact
    return {
        **artifact.safe_metadata(),
        "image_id": artifact.image_id,
        "storage_name": stored.storage_name,
        "owner_hashes": sorted(stored.owner_hashes),
        "producer_pid": int(stored.producer_pid),
        "producer_identity": stored.producer_identity,
        "leases": dict(stored.leases),
        "owner_scope": stored.owner_scope,
    }


def _persist_stored_image(stored: _StoredImage) -> None:
    _atomic_write(
        _artifact_metadata_path(stored.artifact.image_id),
        json.dumps(
            _artifact_store_payload(stored),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def _delete_stored_media(metadata: dict) -> None:
    root = _artifact_root().resolve(strict=False)
    storage_name = str(metadata.get("storage_name") or "")
    if storage_name and Path(storage_name).name == storage_name:
        data_path = (root / storage_name).resolve(strict=False)
        if data_path.parent == root:
            try:
                data_path.unlink(missing_ok=True)
            except OSError:
                pass
    image_id = str(metadata.get("image_id") or "")
    if image_id:
        try:
            _artifact_metadata_path(image_id).unlink(missing_ok=True)
        except OSError:
            pass


def _validate_reference_metadata(
    artifact: ImageArtifact,
    reference: dict | None,
) -> None:
    if not isinstance(reference, dict):
        return
    expected = {
        "image_id": artifact.image_id,
        "mime": artifact.mime_type,
        "width": artifact.width,
        "height": artifact.height,
        "content_hash": artifact.content_hash,
        "source_type": artifact.source_type,
        "byte_size": len(artifact.image_bytes),
        "created_at": artifact.created_at,
        "lifecycle": artifact.lifecycle,
        "expires_at": artifact.expires_at,
        "parent_image_id": artifact.parent_image_id,
        "detail": artifact.detail_level,
        "capture_method": artifact.capture_method,
        "source_pixel_format": artifact.source_pixel_format,
        "pixel_format_conversion": artifact.pixel_format_conversion,
        "alpha_handling": artifact.alpha_handling,
        "source_color_space": artifact.source_color_space,
        "color_space_conversion": artifact.color_space_conversion,
        "fallback_used": artifact.fallback_used,
    }
    if any(
        reference.get(key) not in (None, "")
        and reference.get(key) != value
        for key, value in expected.items()
    ):
        raise ImageInputError(
            "ERROR: an attached image reference does not match its stored "
            "metadata; no provider request was sent.",
            reason_code="IMAGE_REF_METADATA_MISMATCH",
        )
    privacy = reference.get("privacy")
    if isinstance(privacy, dict) and (
        privacy.get("class", artifact.privacy_class) != artifact.privacy_class
        or privacy.get("metadata_stripped", artifact.metadata_stripped)
        is not artifact.metadata_stripped
    ):
        raise ImageInputError(
            "ERROR: an attached image reference does not match its stored "
            "privacy metadata; no provider request was sent.",
            reason_code="IMAGE_REF_METADATA_MISMATCH",
        )
    if (
        isinstance(reference.get("capture_attempts"), list)
        and reference.get("capture_attempts")
        != [dict(item) for item in artifact.capture_attempts]
    ):
        raise ImageInputError(
            "ERROR: an attached image reference does not match its capture "
            "provenance; no provider request was sent.",
            reason_code="IMAGE_REF_METADATA_MISMATCH",
        )


@dataclass(frozen=True)
class MediaExportDecision:
    """Fail-closed decision for optional image input on the active model."""

    provider_capable: bool
    exact_source_authorized: bool
    media_export_authorized: bool
    canonical_reencoded: bool
    same_provider: bool
    usable: bool
    reason: str


def text_content(content) -> str:
    """Return only textual parts from one OpenAI-style message content value."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
        and part.get("text")
    )


def historical_image_reference_requested(text: object) -> bool:
    """Return whether the current turn refers to the latest prior image."""

    return bool(_HISTORICAL_IMAGE_REFERENCE.search(str(text or "").strip()))


def _validated_image_geometry(data: bytes, declared_mime: str) -> tuple[int, int]:
    """Validate the complete image and return bounded decoded dimensions."""

    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as opened:
            detected = {
                "GIF": "image/gif",
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }.get(str(opened.format or "").upper())
            if detected != declared_mime:
                raise ImageInputError(
                    "ERROR: an attached image MIME type does not match its "
                    "decoded format; no provider request was sent."
                )
            width, height = int(opened.width), int(opened.height)
            if declared_mime == "image/gif" and int(
                getattr(opened, "n_frames", 1)
            ) != 1:
                raise ImageInputError(
                    "ERROR: animated GIF image input is not supported; "
                    "no provider request was sent."
                )
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise ImageInputError(
                    "ERROR: an attached image has invalid or excessive "
                    "dimensions; no provider request was sent."
                )
            opened.load()
    except ImageInputError:
        raise
    except Exception as exc:
        raise ImageInputError(
            "ERROR: an attached image is invalid; no provider request was sent."
        ) from exc
    return width, height


def create_image_artifact(
    data: bytes,
    mime_type: str,
    source_type: str | ImageSourceType,
    *,
    parent_image_id: str = "",
    created_at: float | None = None,
    hwnd: str = "",
    surface_id: str = "",
    generation: str = "",
    application_identity: str = "",
    bounds: tuple[int, int, int, int] | None = None,
    dpi: int = 0,
    monitor: str = "",
    freshness: str = "",
    source_dimensions: tuple[int, int] | None = None,
    provider_dimensions: tuple[int, int] | None = None,
    detail_level: str = "auto",
    crop_transform: dict | None = None,
    maximum_bytes: int | None = None,
    lifecycle: str | None = None,
    expires_at: float | None = None,
    privacy_class: str = "private",
    metadata_stripped: bool | None = None,
    capture_method: str = "",
    source_pixel_format: str = "",
    pixel_format_conversion: str = "",
    alpha_handling: str = "",
    source_color_space: str = "",
    color_space_conversion: str = "",
    capture_attempts: Iterable[dict] = (),
    fallback_used: bool = False,
) -> ImageArtifact:
    """Create one canonical artifact after byte, format and geometry proof."""

    if not isinstance(data, bytes) or not data:
        raise ImageInputError(
            "ERROR: an attached image is empty; no provider request was sent."
        )
    effective_source_limit = min(
        MAX_SOURCE_IMAGE_BYTES,
        int(maximum_bytes) if maximum_bytes is not None else MAX_SOURCE_IMAGE_BYTES,
    )
    if len(data) > effective_source_limit:
        raise ImageInputError(
            "ERROR: an attached image exceeds the provider size limit; "
            "no provider request was sent."
        )
    mime = str(mime_type or "").lower()
    if mime not in _ALL_OSIA_IMAGE_MIMES or _magic_mime(data) != mime:
        raise ImageInputError(
            "ERROR: an attached image MIME type does not match its bytes; "
            "no provider request was sent."
        )
    source = (
        source_type.value
        if isinstance(source_type, ImageSourceType)
        else str(source_type or "")
    )
    if source not in {item.value for item in ImageSourceType}:
        raise ImageInputError(
            "ERROR: an attached image source is invalid; no provider request "
            "was sent."
        )
    width, height = _validated_image_geometry(data, mime)
    digest = hashlib.sha256(data).hexdigest()
    created = float(time.time() if created_at is None else created_at)
    default_lifecycle = (
        "durable_backed"
        if source == ImageSourceType.USER_UPLOAD.value else "ephemeral"
    )
    normalized_lifecycle = str(lifecycle or default_lifecycle)
    if normalized_lifecycle not in {"ephemeral", "durable_backed"}:
        raise ImageInputError(
            "ERROR: an attached image lifecycle is invalid; no provider request "
            "was sent.",
            reason_code="IMAGE_LIFECYCLE_INVALID",
        )
    expiry = float(
        expires_at
        if expires_at is not None else
        created + _SOURCE_TTL_SECONDS[source]
    )
    if expiry <= created:
        raise ImageInputError(
            "ERROR: an attached image reference has expired; no provider request "
            "was sent.",
            reason_code="IMAGE_REF_EXPIRED",
        )
    normalized_bounds = (
        tuple(int(value) for value in bounds) if bounds is not None else None
    )
    normalized_crop = dict(crop_transform) if crop_transform else None
    normalized_capture_attempts = tuple(
        dict(item) for item in capture_attempts
        if isinstance(item, dict)
    )[:8]
    identity_payload = {
                "source_type": source,
                "content_hash": digest,
                "created_at": created,
                "parent_image_id": str(parent_image_id or ""),
                "hwnd": str(hwnd or ""),
                "surface_id": str(surface_id or ""),
                "generation": str(generation or ""),
                "application_identity": str(application_identity or ""),
                "bounds": normalized_bounds,
                "dpi": max(0, int(dpi or 0)),
                "monitor": str(monitor or ""),
                "crop_transform": normalized_crop,
    }
    capture_provenance = {
        "capture_method": str(capture_method or "")[:80],
        "source_pixel_format": str(source_pixel_format or "")[:80],
        "pixel_format_conversion": str(
            pixel_format_conversion or ""
        )[:160],
        "alpha_handling": str(alpha_handling or "")[:160],
        "source_color_space": str(source_color_space or "")[:120],
        "color_space_conversion": str(
            color_space_conversion or ""
        )[:160],
        "fallback_used": bool(fallback_used),
        "capture_attempts": [
            dict(item) for item in normalized_capture_attempts
        ],
    }
    if any(
        value for key, value in capture_provenance.items()
        if key not in {"fallback_used", "capture_attempts"}
    ) or capture_provenance["fallback_used"]:
        identity_payload["capture_provenance"] = capture_provenance
    identity = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]
    return ImageArtifact(
        image_id=f"img_{identity}",
        source_type=source,
        mime_type=mime,
        width=width,
        height=height,
        content_hash=digest,
        image_bytes=data,
        created_at=created,
        parent_image_id=str(parent_image_id or ""),
        hwnd=str(hwnd or ""),
        surface_id=str(surface_id or ""),
        generation=str(generation or ""),
        application_identity=str(application_identity or ""),
        bounds=normalized_bounds,
        dpi=max(0, int(dpi or 0)),
        monitor=str(monitor or ""),
        freshness=str(freshness or ""),
        source_dimensions=source_dimensions or (width, height),
        provider_dimensions=provider_dimensions or (width, height),
        detail_level=str(detail_level or "auto"),
        crop_transform=normalized_crop,
        lifecycle=normalized_lifecycle,
        expires_at=expiry,
        privacy_class=(
            str(privacy_class or "private")
            if str(privacy_class or "private") in {"private", "sensitive"}
            else "private"
        ),
        metadata_stripped=(
            source != ImageSourceType.USER_UPLOAD.value
            if metadata_stripped is None else bool(metadata_stripped)
        ),
        capture_method=capture_provenance["capture_method"],
        source_pixel_format=capture_provenance["source_pixel_format"],
        pixel_format_conversion=capture_provenance[
            "pixel_format_conversion"
        ],
        alpha_handling=capture_provenance["alpha_handling"],
        source_color_space=capture_provenance["source_color_space"],
        color_space_conversion=capture_provenance[
            "color_space_conversion"
        ],
        capture_attempts=normalized_capture_attempts,
        fallback_used=capture_provenance["fallback_used"],
    )


def register_image_artifact(
    artifact: ImageArtifact,
    *,
    owner_id: str = "",
) -> dict:
    """Retain pixels in the private owner-aware shared store."""

    if not isinstance(artifact, ImageArtifact):
        raise TypeError("artifact must be an ImageArtifact")
    global _IMAGE_STORE_BYTES
    effective_owner = _effective_owner_id(owner_id)
    owner_hash = _owner_hash(effective_owner)
    owner_scope = (
        "turn" if effective_owner.startswith("turn-media\0") else
        "process" if effective_owner.startswith("process-media\0") else
        "conversation"
    )
    with _IMAGE_STORE_LOCK, _media_process_lock():
        _private_directory(_artifact_root())
        previous = _IMAGE_STORE.pop(artifact.image_id, None)
        existing = _load_artifact_metadata(artifact.image_id)
        owner_hashes = set(
            existing.get("owner_hashes") or []
            if isinstance(existing, dict) else []
        )
        if previous is not None:
            _IMAGE_STORE_BYTES -= len(previous.artifact.image_bytes)
            owner_hashes.update(previous.owner_hashes)
        if owner_hash:
            owner_hashes.add(owner_hash)
        storage_name = str(
            (existing or {}).get("storage_name")
            or f"media-{secrets.token_urlsafe(24)}.bin"
        )
        data_path = _artifact_root() / storage_name
        if data_path.exists():
            try:
                existing_bytes = data_path.read_bytes()
            except OSError as exc:
                raise ImageInputError(
                    "ERROR: the private image store is unavailable; no provider "
                    "request was sent.",
                    reason_code="IMAGE_STORE_UNAVAILABLE",
                ) from exc
            if hashlib.sha256(existing_bytes).hexdigest() != artifact.content_hash:
                raise ImageInputError(
                    "ERROR: an attached image reference does not match its stored "
                    "bytes; no provider request was sent.",
                    reason_code="IMAGE_REF_METADATA_MISMATCH",
                )
        else:
            _atomic_write(data_path, artifact.image_bytes)
        stored = _StoredImage(
            artifact=artifact,
            owner_hashes=frozenset(owner_hashes),
            producer_pid=int((existing or {}).get("producer_pid") or os.getpid()),
            producer_identity=str(
                (existing or {}).get("producer_identity")
                or _process_identity()
            ),
            storage_name=storage_name,
            leases=_active_leases(existing or {}),
            owner_scope=str(
                (existing or {}).get("owner_scope") or owner_scope
            ),
        )
        _persist_stored_image(stored)
        _IMAGE_STORE[artifact.image_id] = stored
        _IMAGE_STORE_BYTES += len(artifact.image_bytes)
        while (
            len(_IMAGE_STORE) > MAX_EPHEMERAL_IMAGES
            or _IMAGE_STORE_BYTES > MAX_EPHEMERAL_IMAGE_BYTES
        ):
            evicted = False
            for key, candidate in tuple(_IMAGE_STORE.items()):
                if candidate.leases:
                    continue
                _IMAGE_STORE.pop(key, None)
                _IMAGE_STORE_BYTES -= len(candidate.artifact.image_bytes)
                evicted = True
                break
            if not evicted:
                break
    reference = artifact.safe_reference()
    reference["owner_bound"] = True
    reference["owner_scope"] = stored.owner_scope
    cleanup_media_store(preserve_image_ids=(artifact.image_id,))
    return reference


def resolve_image_artifact(
    image_id: str,
    *,
    owner_id: str = "",
    reference: dict | None = None,
) -> ImageArtifact:
    """Resolve one exact owner-bound ref from memory or private shared backing."""

    key = str(image_id or "")
    if not key:
        raise ImageInputError(
            "ERROR: an attached image reference is unavailable; no provider "
            "request was sent.",
            reason_code="IMAGE_REF_UNAVAILABLE",
        )
    effective_owner = _effective_owner_id(owner_id)
    owner_hash = _owner_hash(effective_owner)
    with _IMAGE_STORE_LOCK, _media_process_lock():
        entry = _IMAGE_STORE.get(key)
        metadata = _load_artifact_metadata(key)
        explicit_owner_binding = bool(
            metadata is not None and "owner_hashes" in metadata
        )
        if entry is not None:
            artifact = entry.artifact
            owner_hashes = (
                frozenset(
                    str(item)
                    for item in (metadata.get("owner_hashes") or [])
                )
                if explicit_owner_binding else
                entry.owner_hashes
            )
            leases = (
                _active_leases(metadata)
                if metadata is not None else
                {
                    token: expiry for token, expiry in entry.leases.items()
                    if expiry > time.time()
                }
            )
            producer_pid = int(
                (metadata or {}).get("producer_pid") or entry.producer_pid
            )
            producer_identity = str(
                (metadata or {}).get("producer_identity")
                or entry.producer_identity
            )
            entry.owner_hashes = owner_hashes
            entry.leases = dict(leases)
        elif metadata is not None:
            owner_hashes = frozenset(
                str(item) for item in metadata.get("owner_hashes") or []
            )
            leases = _active_leases(metadata)
            producer_pid = int(metadata.get("producer_pid") or 0)
            producer_identity = str(metadata.get("producer_identity") or "")
            expires_at = float(metadata.get("expires_at") or 0.0)
            if expires_at <= time.time() and not leases:
                raise ImageInputError(
                    "ERROR: an attached image reference has expired; no provider "
                    "request was sent.",
                    reason_code="IMAGE_REF_EXPIRED",
                )
            if (
                str(metadata.get("lifecycle") or "ephemeral") == "ephemeral"
                and not leases
                and not _producer_is_alive(producer_pid, producer_identity)
            ):
                raise ImageInputError(
                    "ERROR: an attached image reference has expired; no provider "
                    "request was sent.",
                    reason_code="IMAGE_REF_EXPIRED",
                )
            storage_name = str(metadata.get("storage_name") or "")
            if not storage_name or Path(storage_name).name != storage_name:
                raise ImageInputError(
                    "ERROR: an attached image reference is unavailable; no "
                    "provider request was sent.",
                    reason_code="IMAGE_REF_UNAVAILABLE",
                )
            root = _artifact_root().resolve(strict=False)
            data_path = (root / storage_name).resolve(strict=False)
            if data_path.parent != root:
                raise ImageInputError(
                    "ERROR: an attached image reference is unavailable; no "
                    "provider request was sent.",
                    reason_code="IMAGE_REF_UNAVAILABLE",
                )
            try:
                image_bytes = data_path.read_bytes()
            except OSError as exc:
                raise ImageInputError(
                    "ERROR: an attached image reference is unavailable; no "
                    "provider request was sent.",
                    reason_code="IMAGE_REF_UNAVAILABLE",
                ) from exc
            if (
                hashlib.sha256(image_bytes).hexdigest()
                != str(metadata.get("content_hash") or "")
                or len(image_bytes) != int(metadata.get("byte_size") or 0)
            ):
                raise ImageInputError(
                    "ERROR: an attached image reference does not match its stored "
                    "bytes; no provider request was sent.",
                    reason_code="IMAGE_REF_METADATA_MISMATCH",
                )
            artifact = create_image_artifact(
                image_bytes,
                str(metadata.get("mime") or ""),
                str(metadata.get("source_type") or ""),
                parent_image_id=str(metadata.get("parent_image_id") or ""),
                created_at=float(metadata.get("created_at") or 0.0),
                hwnd=str(metadata.get("hwnd") or ""),
                surface_id=str(metadata.get("surface_id") or ""),
                generation=str(metadata.get("generation") or ""),
                application_identity=str(
                    metadata.get("application_identity") or ""
                ),
                bounds=(
                    tuple(metadata.get("bounds"))
                    if isinstance(metadata.get("bounds"), list) else None
                ),
                dpi=int(metadata.get("dpi") or 0),
                monitor=str(metadata.get("monitor") or ""),
                freshness=str(metadata.get("freshness") or ""),
                source_dimensions=tuple(
                    metadata.get("source_dimensions")
                    or (metadata.get("width"), metadata.get("height"))
                ),
                provider_dimensions=tuple(
                    metadata.get("provider_dimensions")
                    or (metadata.get("width"), metadata.get("height"))
                ),
                detail_level=str(metadata.get("detail") or "auto"),
                crop_transform=(
                    dict(metadata.get("crop_transform"))
                    if isinstance(metadata.get("crop_transform"), dict) else None
                ),
                lifecycle=str(metadata.get("lifecycle") or "ephemeral"),
                expires_at=expires_at,
                privacy_class=str(
                    (
                        metadata.get("privacy")
                        if isinstance(metadata.get("privacy"), dict) else {}
                    ).get("class") or "private"
                ),
                metadata_stripped=(
                    (
                        metadata.get("privacy")
                        if isinstance(metadata.get("privacy"), dict) else {}
                    ).get("metadata_stripped") is True
                ),
                capture_method=str(metadata.get("capture_method") or ""),
                source_pixel_format=str(
                    metadata.get("source_pixel_format") or ""
                ),
                pixel_format_conversion=str(
                    metadata.get("pixel_format_conversion") or ""
                ),
                alpha_handling=str(metadata.get("alpha_handling") or ""),
                source_color_space=str(
                    metadata.get("source_color_space") or ""
                ),
                color_space_conversion=str(
                    metadata.get("color_space_conversion") or ""
                ),
                capture_attempts=(
                    metadata.get("capture_attempts")
                    if isinstance(metadata.get("capture_attempts"), list)
                    else ()
                ),
                fallback_used=metadata.get("fallback_used") is True,
            )
            if artifact.image_id != key:
                raise ImageInputError(
                    "ERROR: an attached image reference does not match its stored "
                    "metadata; no provider request was sent.",
                    reason_code="IMAGE_REF_METADATA_MISMATCH",
                )
            entry = _StoredImage(
                artifact=artifact,
                owner_hashes=owner_hashes,
                producer_pid=producer_pid,
                producer_identity=producer_identity,
                storage_name=storage_name,
                leases=leases,
                owner_scope=str(
                    metadata.get("owner_scope") or "conversation"
                ),
            )
            global _IMAGE_STORE_BYTES
            _IMAGE_STORE[key] = entry
            _IMAGE_STORE_BYTES += len(artifact.image_bytes)
        else:
            raise ImageInputError(
                "ERROR: an attached image reference is unavailable; no provider "
                "request was sent.",
                reason_code="IMAGE_REF_UNAVAILABLE",
            )
        if owner_hashes and owner_hash not in owner_hashes:
            raise ImageInputError(
                "ERROR: an attached image belongs to another conversation; no "
                "provider request was sent.",
                reason_code="IMAGE_OWNER_MISMATCH",
            )
        if not owner_hashes and explicit_owner_binding:
            raise ImageInputError(
                "ERROR: an attached image has no active conversation owner; no "
                "provider request was sent.",
                reason_code="IMAGE_OWNER_MISMATCH",
            )
        if not owner_hashes and (
            producer_pid != os.getpid()
            or producer_identity not in {"", _process_identity()}
        ):
            # Old ownerless records remain usable only by their exact producer
            # process. They never become a cross-process public capability.
            raise ImageInputError(
                "ERROR: an attached image belongs to another process; no "
                "provider request was sent.",
                reason_code="IMAGE_OWNER_MISMATCH",
            )
        if artifact.expires_at <= time.time() and not leases:
            raise ImageInputError(
                "ERROR: an attached image reference has expired; no provider "
                "request was sent.",
                reason_code="IMAGE_REF_EXPIRED",
            )
        if (
            artifact.lifecycle == "ephemeral"
            and not leases
            and not _producer_is_alive(producer_pid, producer_identity)
        ):
            raise ImageInputError(
                "ERROR: an attached image reference has expired; no provider "
                "request was sent.",
                reason_code="IMAGE_REF_EXPIRED",
            )
        if (
            isinstance(reference, dict)
            and reference.get("owner_scope")
            and str(reference.get("owner_scope"))
            != str((metadata or {}).get("owner_scope") or "conversation")
        ):
            raise ImageInputError(
                "ERROR: an attached image reference does not match its owner "
                "scope; no provider request was sent.",
                reason_code="IMAGE_REF_METADATA_MISMATCH",
            )
        _validate_reference_metadata(artifact, reference)
        _IMAGE_STORE.move_to_end(artifact.image_id)
        return artifact


def resolve_image_reference(reference: dict, *, owner_id: str = "") -> ImageArtifact:
    """Resolve and verify every immutable field carried by one safe reference."""

    if not isinstance(reference, dict):
        raise ImageInputError(
            "ERROR: an attached image reference is unavailable; no provider "
            "request was sent.",
            reason_code="IMAGE_REF_UNAVAILABLE",
        )
    return resolve_image_artifact(
        str(reference.get("image_id") or ""),
        owner_id=owner_id,
        reference=reference,
    )


def reference_owner_id(
    reference: dict,
    *,
    conversation_id: str,
    turn_id: str = "",
) -> str:
    """Resolve the exact owner class carried by one opaque reference."""

    scope = str(
        reference.get("owner_scope") if isinstance(reference, dict) else ""
    )
    if scope == "turn":
        return turn_scoped_media_owner(conversation_id, turn_id)
    if scope == "process":
        return ""
    return str(conversation_id or "")


def acquire_image_lease(
    reference: dict,
    *,
    owner_id: str,
    lease_owner_id: str,
    ttl_seconds: float = MEDIA_LEASE_SECONDS,
) -> str:
    """Pin exact bytes while one provider envelope is being committed."""

    artifact = resolve_image_reference(reference, owner_id=owner_id)
    lease_owner = str(lease_owner_id or "").strip()
    if not lease_owner:
        raise ImageInputError(
            "ERROR: an image delivery lease has no owner; no provider request "
            "was sent.",
            reason_code="IMAGE_LEASE_INVALID",
        )
    lease_id = "lease_" + secrets.token_urlsafe(24)
    expiry = time.time() + max(1.0, min(float(ttl_seconds), 60 * 60))
    with _IMAGE_STORE_LOCK, _media_process_lock():
        metadata = _load_artifact_metadata(artifact.image_id)
        if metadata is None:
            raise ImageInputError(
                "ERROR: an attached image reference is unavailable; no provider "
                "request was sent.",
                reason_code="IMAGE_REF_UNAVAILABLE",
            )
        leases = _active_leases(metadata)
        leases[lease_id] = expiry
        metadata["leases"] = leases
        metadata.setdefault("lease_owners", {})[lease_id] = _owner_hash(
            lease_owner
        )
        _atomic_write(
            _artifact_metadata_path(artifact.image_id),
            json.dumps(
                metadata,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        entry = _IMAGE_STORE.get(artifact.image_id)
        if entry is not None:
            entry.leases = dict(leases)
    return lease_id


def release_image_lease(image_id: str, lease_id: str) -> bool:
    key = str(image_id or "")
    token = str(lease_id or "")
    if not key or not token:
        return False
    with _IMAGE_STORE_LOCK, _media_process_lock():
        metadata = _load_artifact_metadata(key)
        if metadata is None:
            return False
        leases = _active_leases(metadata)
        existed = token in leases or token in (metadata.get("leases") or {})
        leases.pop(token, None)
        metadata["leases"] = leases
        owners = metadata.get("lease_owners")
        if isinstance(owners, dict):
            owners.pop(token, None)
            metadata["lease_owners"] = owners
        explicitly_ownerless = bool(
            "owner_hashes" in metadata
            and not (metadata.get("owner_hashes") or [])
        )
        if explicitly_ownerless and not leases:
            _delete_stored_media(metadata)
            entry = _IMAGE_STORE.pop(key, None)
            if entry is not None:
                global _IMAGE_STORE_BYTES
                _IMAGE_STORE_BYTES -= len(entry.artifact.image_bytes)
        else:
            _atomic_write(
                _artifact_metadata_path(key),
                json.dumps(
                    metadata,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            entry = _IMAGE_STORE.get(key)
            if entry is not None:
                entry.leases = dict(leases)
        return existed


def release_image_owner(image_id: str, *, owner_id: str) -> bool:
    """Release one conversation/turn owner without removing active leases."""

    key = str(image_id or "")
    owner_hash = _owner_hash(owner_id) if owner_id else ""
    if not key or not owner_hash:
        return False
    with _IMAGE_STORE_LOCK, _media_process_lock():
        metadata = _load_artifact_metadata(key)
        if metadata is None:
            return False
        owners = set(str(item) for item in metadata.get("owner_hashes") or [])
        if owner_hash not in owners:
            return False
        owners.remove(owner_hash)
        metadata["owner_hashes"] = sorted(owners)
        leases = _active_leases(metadata)
        if not owners and not leases:
            _delete_stored_media(metadata)
            entry = _IMAGE_STORE.pop(key, None)
            if entry is not None:
                global _IMAGE_STORE_BYTES
                _IMAGE_STORE_BYTES -= len(entry.artifact.image_bytes)
        else:
            _atomic_write(
                _artifact_metadata_path(key),
                json.dumps(
                    metadata,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            entry = _IMAGE_STORE.get(key)
            if entry is not None:
                entry.owner_hashes = frozenset(owners)
                entry.leases = dict(leases)
        return True


def release_media_owner(owner_id: str) -> int:
    """Release one owner from every bounded private artifact record."""

    owner_hash = _owner_hash(owner_id) if owner_id else ""
    if not owner_hash:
        return 0
    released = 0
    with _media_process_lock():
        _private_directory(_artifact_root())
        image_ids = []
        for path in sorted(_artifact_root().glob("*.json"))[:1024]:
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(metadata, dict)
                and owner_hash in (metadata.get("owner_hashes") or [])
                and metadata.get("image_id")
            ):
                image_ids.append(str(metadata["image_id"]))
    for image_id in image_ids:
        if release_image_owner(image_id, owner_id=owner_id):
            released += 1
    return released


class MediaEnvelopeType(str, Enum):
    LOCAL_IMAGE = "localImage"
    INPUT_IMAGE = "inputImage"
    TURN_STEER_IMAGE = "turnSteerImage"


class MediaDeliveryState(str, Enum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    COMMITTED = "COMMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"
    COMMIT_UNKNOWN = "COMMIT_UNKNOWN"


@dataclass(frozen=True)
class MediaDelivery:
    delivery_id: str
    image_id: str
    provider: str
    model: str
    conversation_id: str
    request_id: str
    turn_id: str
    tool_call_id: str
    rpc_id: str
    envelope_type: str
    state: str
    content_hash: str
    prepared_at: float
    committed_at: float = 0.0
    acknowledged_at: float = 0.0
    failed_at: float = 0.0
    failure_reason: str = ""
    lease_id: str = ""
    transport_id: str = ""
    send_authorized: bool = field(default=True, compare=False, repr=False)

    def safe_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("send_authorized", None)
        return payload


def _delivery_identity(
    *,
    image_id: str,
    provider: str,
    model: str,
    conversation_id: str,
    request_id: str,
    turn_id: str,
    tool_call_id: str,
    rpc_id: str,
    envelope_type: str,
    transport_id: str = "",
) -> str:
    return hashlib.sha256(json.dumps(
        {
            "image_id": image_id,
            "provider": provider,
            "model": model,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "rpc_id": rpc_id,
            "envelope_type": envelope_type,
            "transport_id": transport_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _delivery_path(delivery_id: str) -> Path:
    digest = hashlib.sha256(str(delivery_id or "").encode("utf-8")).hexdigest()
    return _delivery_root() / f"{digest}.json"


def _load_delivery(delivery_id: str) -> MediaDelivery | None:
    try:
        payload = json.loads(
            _delivery_path(delivery_id).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    try:
        return MediaDelivery(**payload)
    except (TypeError, ValueError):
        return None


def media_delivery_record(delivery_id: str) -> MediaDelivery | None:
    with _media_process_lock():
        return _load_delivery(str(delivery_id or ""))


def prepare_media_delivery(
    reference: dict,
    *,
    owner_id: str,
    provider: str,
    model: str,
    conversation_id: str,
    request_id: str,
    turn_id: str,
    envelope_type: str | MediaEnvelopeType,
    tool_call_id: str = "",
    rpc_id: str = "",
    transport_id: str = "",
) -> MediaDelivery:
    """Prepare one envelope identity and pin bytes before transport commit."""

    artifact = resolve_image_reference(reference, owner_id=owner_id)
    envelope = (
        envelope_type.value
        if isinstance(envelope_type, MediaEnvelopeType) else str(envelope_type)
    )
    if envelope not in {item.value for item in MediaEnvelopeType}:
        raise ImageInputError(
            "ERROR: an image delivery envelope is invalid; no provider request "
            "was sent.",
            reason_code="IMAGE_DELIVERY_FAILED",
        )
    identity = _delivery_identity(
        image_id=artifact.image_id,
        provider=str(provider or ""),
        model=str(model or ""),
        conversation_id=str(conversation_id or ""),
        request_id=str(request_id or ""),
        turn_id=str(turn_id or ""),
        tool_call_id=str(tool_call_id or ""),
        rpc_id=str(rpc_id or ""),
        envelope_type=envelope,
        transport_id=str(transport_id or ""),
    )
    delivery_id = "delivery_" + identity[:32]
    with _IMAGE_STORE_LOCK, _media_process_lock():
        existing = _load_delivery(delivery_id)
        if existing is not None:
            if existing.state in {
                MediaDeliveryState.DISPATCHING.value,
                MediaDeliveryState.COMMITTED.value,
                MediaDeliveryState.ACKNOWLEDGED.value,
                MediaDeliveryState.COMMIT_UNKNOWN.value,
            }:
                return MediaDelivery(
                    **existing.safe_dict(), send_authorized=False,
                )
            if (
                existing.state == MediaDeliveryState.PREPARED.value
                and existing.prepared_at + MEDIA_LEASE_SECONDS > time.time()
            ):
                return MediaDelivery(
                    **existing.safe_dict(), send_authorized=False,
                )

        metadata = _load_artifact_metadata(artifact.image_id)
        if metadata is None:
            raise ImageInputError(
                "ERROR: an attached image reference is unavailable; no provider "
                "request was sent.",
                reason_code="IMAGE_REF_UNAVAILABLE",
            )
        owner_hash = _owner_hash(_effective_owner_id(owner_id))
        owner_hashes = set(str(item) for item in metadata.get("owner_hashes") or [])
        if owner_hashes and owner_hash not in owner_hashes:
            raise ImageInputError(
                "ERROR: an attached image belongs to another conversation; no "
                "provider request was sent.",
                reason_code="IMAGE_OWNER_MISMATCH",
            )
        lease_id = "lease_" + secrets.token_urlsafe(24)
        leases = _active_leases(metadata)
        leases[lease_id] = time.time() + MEDIA_LEASE_SECONDS
        metadata["leases"] = leases
        metadata.setdefault("lease_owners", {})[lease_id] = _owner_hash(
            delivery_id
        )
        _atomic_write(
            _artifact_metadata_path(artifact.image_id),
            json.dumps(
                metadata,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        entry = _IMAGE_STORE.get(artifact.image_id)
        if entry is not None:
            entry.leases = dict(leases)

        delivery = MediaDelivery(
            delivery_id=delivery_id,
            image_id=artifact.image_id,
            provider=str(provider or ""),
            model=str(model or ""),
            conversation_id=str(conversation_id or ""),
            request_id=str(request_id or ""),
            turn_id=str(turn_id or ""),
            tool_call_id=str(tool_call_id or ""),
            rpc_id=str(rpc_id or ""),
            envelope_type=envelope,
            state=MediaDeliveryState.PREPARED.value,
            content_hash=artifact.content_hash,
            prepared_at=time.time(),
            lease_id=lease_id,
            transport_id=str(transport_id or ""),
        )
        _private_directory(_delivery_root())
        _atomic_write(
            _delivery_path(delivery_id),
            json.dumps(
                delivery.safe_dict(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    return delivery


def _transition_media_delivery(
    delivery_id: str,
    state: MediaDeliveryState,
    *,
    reason_code: str = "",
) -> MediaDelivery | None:
    with _media_process_lock():
        delivery = _load_delivery(delivery_id)
        if delivery is None:
            return None
        current = MediaDeliveryState(delivery.state)
        allowed = {
            MediaDeliveryState.PREPARED: {
                MediaDeliveryState.DISPATCHING,
                MediaDeliveryState.COMMITTED,
                MediaDeliveryState.ACKNOWLEDGED,
                MediaDeliveryState.FAILED,
                MediaDeliveryState.COMMIT_UNKNOWN,
            },
            MediaDeliveryState.DISPATCHING: {
                MediaDeliveryState.COMMITTED,
                MediaDeliveryState.ACKNOWLEDGED,
                MediaDeliveryState.FAILED,
                MediaDeliveryState.COMMIT_UNKNOWN,
            },
            MediaDeliveryState.COMMITTED: {
                MediaDeliveryState.ACKNOWLEDGED,
            },
            MediaDeliveryState.ACKNOWLEDGED: set(),
            MediaDeliveryState.FAILED: set(),
            MediaDeliveryState.COMMIT_UNKNOWN: {
                MediaDeliveryState.ACKNOWLEDGED,
            },
        }
        if current is state:
            return delivery
        if state not in allowed[current]:
            return delivery
        now = time.time()
        payload = delivery.safe_dict()
        payload["state"] = state.value
        if state is MediaDeliveryState.COMMITTED:
            payload["committed_at"] = now
        elif state is MediaDeliveryState.ACKNOWLEDGED:
            payload["acknowledged_at"] = now
        elif state is MediaDeliveryState.FAILED:
            payload["failed_at"] = now
            payload["failure_reason"] = str(reason_code or "")[:160]
        elif state is MediaDeliveryState.COMMIT_UNKNOWN:
            payload["failed_at"] = now
            payload["failure_reason"] = str(
                reason_code or "IMAGE_DELIVERY_COMMIT_UNKNOWN"
            )[:160]
        transitioned = MediaDelivery(**payload)
        _atomic_write(
            _delivery_path(delivery_id),
            json.dumps(
                transitioned.safe_dict(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    if delivery.lease_id and state in {
        MediaDeliveryState.COMMITTED,
        MediaDeliveryState.ACKNOWLEDGED,
        MediaDeliveryState.FAILED,
        MediaDeliveryState.COMMIT_UNKNOWN,
    }:
        release_image_lease(delivery.image_id, delivery.lease_id)
    return transitioned


def commit_media_delivery(delivery_id: str) -> MediaDelivery | None:
    return _transition_media_delivery(
        delivery_id, MediaDeliveryState.COMMITTED,
    )


def begin_media_delivery(delivery_id: str) -> MediaDelivery | None:
    """Durably freeze dedup before provider transport can observe bytes."""

    return _transition_media_delivery(
        delivery_id, MediaDeliveryState.DISPATCHING,
    )


def acknowledge_media_delivery(delivery_id: str) -> MediaDelivery | None:
    return _transition_media_delivery(
        delivery_id, MediaDeliveryState.ACKNOWLEDGED,
    )


def fail_media_delivery(
    delivery_id: str,
    *,
    reason_code: str = "IMAGE_DELIVERY_FAILED",
) -> MediaDelivery | None:
    return _transition_media_delivery(
        delivery_id,
        MediaDeliveryState.FAILED,
        reason_code=reason_code,
    )


def mark_media_delivery_commit_unknown(
    delivery_id: str,
    *,
    reason_code: str = "IMAGE_DELIVERY_COMMIT_UNKNOWN",
) -> MediaDelivery | None:
    return _transition_media_delivery(
        delivery_id,
        MediaDeliveryState.COMMIT_UNKNOWN,
        reason_code=reason_code,
    )


def cleanup_media_store(
    *,
    now: float | None = None,
    preserve_image_ids: Iterable[str] = (),
) -> dict[str, int]:
    """Bounded cleanup of only OSIA-owned expired media and delivery rows."""

    moment = time.time() if now is None else float(now)
    removed_artifacts = 0
    removed_deliveries = 0
    preserved = {
        str(item) for item in preserve_image_ids if str(item or "")
    }
    with _IMAGE_STORE_LOCK, _media_process_lock():
        _private_directory(_artifact_root())
        metadata_files = sorted(
            _artifact_root().glob("*.json"),
            key=lambda path: path.stat().st_mtime,
        )
        retained: list[tuple[Path, dict, int]] = []
        for path in metadata_files:
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict) or not metadata.get("image_id"):
                continue
            leases = _active_leases(metadata, now=moment)
            expired = float(metadata.get("expires_at") or 0.0) <= moment
            ephemeral_dead = bool(
                metadata.get("lifecycle") == "ephemeral"
                and not _producer_is_alive(
                    metadata.get("producer_pid"),
                    metadata.get("producer_identity"),
                )
            )
            size = int(metadata.get("byte_size") or 0)
            if (
                (expired or ephemeral_dead)
                and not leases
                and str(metadata.get("image_id") or "") not in preserved
            ):
                _delete_stored_media(metadata)
                entry = _IMAGE_STORE.pop(str(metadata.get("image_id")), None)
                if entry is not None:
                    global _IMAGE_STORE_BYTES
                    _IMAGE_STORE_BYTES -= len(entry.artifact.image_bytes)
                removed_artifacts += 1
            else:
                if metadata.get("leases") != leases:
                    metadata["leases"] = leases
                    _atomic_write(
                        path,
                        json.dumps(
                            metadata,
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    )
                retained.append((path, metadata, size))
        total = sum(item[2] for item in retained)
        remaining_count = len(retained)
        for _path, metadata, size in retained:
            if (
                remaining_count <= MEDIA_STORE_MAX_FILES
                and total <= MEDIA_STORE_MAX_BYTES
            ):
                break
            if _active_leases(metadata, now=moment):
                continue
            if str(metadata.get("image_id") or "") in preserved:
                continue
            _delete_stored_media(metadata)
            entry = _IMAGE_STORE.pop(str(metadata.get("image_id") or ""), None)
            if entry is not None:
                _IMAGE_STORE_BYTES -= len(entry.artifact.image_bytes)
            total -= size
            remaining_count -= 1
            removed_artifacts += 1

        _private_directory(_delivery_root())
        for path in sorted(_delivery_root().glob("*.json"))[:1024]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                prepared = float(payload.get("prepared_at") or 0.0)
                state = str(payload.get("state") or "")
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                prepared + MEDIA_DELIVERY_RETENTION_SECONDS <= moment
                or (
                    state == MediaDeliveryState.PREPARED.value
                    and prepared + MEDIA_LEASE_SECONDS <= moment
                )
            ):
                try:
                    path.unlink(missing_ok=True)
                    removed_deliveries += 1
                except OSError:
                    pass
    return {
        "removed_artifacts": removed_artifacts,
        "removed_deliveries": removed_deliveries,
    }


def cleanup_owned_temp_images(
    *,
    directories: Iterable[str | Path] = (),
    now: float | None = None,
    minimum_age_seconds: float = 60 * 60,
) -> int:
    """Delete only expired OSIA-owned image temp names from exact roots."""

    moment = time.time() if now is None else float(now)
    supplied_roots = {
        Path(directory).resolve(strict=False)
        for directory in directories if str(directory or "")
    }
    temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
    roots = set(supplied_roots)
    roots.add(temp_root)
    removed = 0
    private_patterns = (
        "vision-*.png", "vision-*.jpg", "vision-*.jpeg", "vision-*.webp",
        "vision-*.gif",
    )
    fixed_patterns = ("osia_kbd_before.png", "osia_kbd_after.png")
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        patterns = (
            (*private_patterns, *fixed_patterns)
            if root in supplied_roots else fixed_patterns
        )
        for pattern in patterns:
            for path in root.glob(pattern):
                try:
                    resolved = path.resolve(strict=True)
                    if (
                        resolved.parent != root
                        or not resolved.is_file()
                        or moment - resolved.stat().st_mtime
                        < minimum_age_seconds
                    ):
                        continue
                    resolved.unlink()
                    removed += 1
                except OSError:
                    continue
    return removed


def image_references(content) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        dict(part)
        for part in content
        if isinstance(part, dict) and part.get("type") == "image_ref"
        and isinstance(part.get("image_id"), str)
    ]


def image_parts(content) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        part for part in content
        if isinstance(part, dict)
        and part.get("type") in {"image_ref", "image_url"}
    ]


def has_image_content(content) -> bool:
    return bool(image_parts(content))


def decode_image_data_url(value, *, maximum_bytes: int | None = None) -> DecodedImage:
    """Decode one bounded data URL and verify its declared MIME by magic bytes."""

    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.startswith("data:image/"):
        raise ImageInputError(
            "ERROR: an attached image is not an inline OSIA image; "
            "no provider request was sent."
        )
    header, separator, payload = value.partition(",")
    if not separator or not header.endswith(";base64"):
        raise ImageInputError(
            "ERROR: an attached image has an invalid data encoding; "
            "no provider request was sent."
        )
    mime = header[5:-7].lower()
    if mime not in _ALL_OSIA_IMAGE_MIMES:
        raise ImageInputError(
            "ERROR: an attached image has an unsupported MIME type; "
            "no provider request was sent."
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError(
            "ERROR: an attached image has invalid base64 data; "
            "no provider request was sent."
        ) from exc
    if not data:
        raise ImageInputError(
            "ERROR: an attached image is empty; no provider request was sent."
        )
    if maximum_bytes is not None and len(data) > maximum_bytes:
        raise ImageInputError(
            "ERROR: an attached image exceeds the provider size limit; "
            "no provider request was sent."
        )
    detected = _magic_mime(data)
    if detected != mime:
        raise ImageInputError(
            "ERROR: an attached image MIME type does not match its bytes; "
            "no provider request was sent."
        )
    width, height = _validated_image_geometry(data, mime)
    return DecodedImage(
        mime=mime,
        data=data,
        width=width,
        height=height,
        content_hash=hashlib.sha256(data).hexdigest(),
    )


def _magic_mime(data: bytes) -> str | None:
    prefix = data[:16]
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def _known_model_vision(provider: str, model: str) -> tuple[bool, bool, bool]:
    from osia.model_capabilities import effective_route_capabilities

    capabilities = effective_route_capabilities(provider, model)
    supported = capabilities.vision_supported is True
    account_permitted = bool(
        provider != "chatgpt_account"
        or (
            capabilities.metadata_known
            and "image" in capabilities.input_modalities
        )
    )
    return capabilities.metadata_known, supported, account_permitted


def image_input_decision(
    provider: str,
    model: str,
    mime_types=(),
) -> ImageInputDecision:
    """Resolve all five capability dimensions for one provider/model/path."""

    provider = str(provider or "").lower()
    model = str(model or "")
    model_known, model_vision, account_permits = _known_model_vision(provider, model)
    adapter = _ADAPTERS.get(provider)
    adapter_supported = adapter is not None
    encoding = adapter[0] if adapter else None
    accepted = adapter[1] if adapter else frozenset()
    formats_ok = all(
        str(mime).lower() in accepted
        or (
            str(mime).lower() in _ALL_OSIA_IMAGE_MIMES
            and "image/png" in accepted
        )
        for mime in mime_types
    )

    if not model_known:
        reason = "the model capability is unknown"
    elif not model_vision:
        reason = "the selected model is text-only"
    elif not adapter_supported:
        reason = "the current provider adapter has no verified image encoding"
    elif not account_permits:
        reason = "the selected account path does not permit image input"
    elif not formats_ok:
        reason = "the attachment format is not accepted by this provider path"
    else:
        reason = "supported"

    usable = bool(
        model_known
        and model_vision
        and adapter_supported
        and account_permits
        and formats_ok
    )
    return ImageInputDecision(
        provider=provider,
        model=model,
        model_known=model_known,
        model_supports_vision=model_vision,
        adapter_supports_images=adapter_supported,
        account_path_permits_images=account_permits,
        formats_accepted=formats_ok,
        encoding=encoding,
        usable=usable,
        reason=reason,
    )


def media_export_decision(
    provider: str,
    model: str,
    mime_type: str,
    *,
    exact_source_authorized: bool,
    media_export_authorized: bool,
    canonical_reencoded: bool,
    same_provider: bool,
) -> MediaExportDecision:
    """Require every WOR-188 export condition; local OCR needs none of them."""

    capability = image_input_decision(provider, model, (mime_type,))
    checks = (
        (capability.usable, capability.reason),
        (exact_source_authorized, "the exact image source is not authorized"),
        (media_export_authorized, "media export permission is not authorized"),
        (canonical_reencoded, "the image was not canonically metadata-stripped"),
        (same_provider, "silent provider switching is forbidden"),
    )
    reason = "supported"
    for allowed, failure in checks:
        if not allowed:
            reason = failure
            break
    return MediaExportDecision(
        provider_capable=capability.usable,
        exact_source_authorized=bool(exact_source_authorized),
        media_export_authorized=bool(media_export_authorized),
        canonical_reencoded=bool(canonical_reencoded),
        same_provider=bool(same_provider),
        usable=all(item[0] for item in checks),
        reason=reason,
    )


def ensure_image_input_supported(
    provider: str,
    model: str,
    content,
    *,
    owner_id: str = "",
) -> ImageInputDecision | None:
    """Validate every image and reject unusable paths before provider execution."""

    parts = image_parts(content)
    if not parts:
        return None
    artifacts = []
    for part in parts:
        if part.get("type") == "image_ref":
            artifacts.append(resolve_image_reference(part, owner_id=owner_id))
            continue
        decoded = decode_image_data_url(part.get("image_url"))
        artifacts.append(create_image_artifact(
            decoded.data,
            decoded.mime,
            ImageSourceType.USER_UPLOAD,
        ))
    decision = image_input_decision(
        provider, model, [item.mime_type for item in artifacts],
    )
    if not decision.usable:
        raise ImageInputError(
            "ERROR: IMAGE_INPUT_UNAVAILABLE: the current provider/model path "
            f"({provider or 'unknown'} / {model or 'unknown'}) cannot analyze "
            f"the attached image: {decision.reason}. No provider request was sent."
        )
    _enforce_provider_image_policy(artifacts, provider)
    return decision


def _normalized_artifact_for_provider(
    artifact: ImageArtifact,
    provider: str,
) -> ImageArtifact:
    adapter = _ADAPTERS.get(str(provider or "").lower())
    accepted = adapter[1] if adapter else frozenset()
    wire_limit = _PROVIDER_WIRE_MAX_BYTES.get(
        str(provider or "").lower(), MAX_NORMALIZED_IMAGE_BYTES,
    )
    if artifact.mime_type in accepted:
        if len(artifact.image_bytes) > wire_limit:
            raise ImageInputError(
                "ERROR: the image exceeds the provider size limit; no provider "
                "request was sent."
            )
        return artifact
    if "image/png" not in accepted:
        raise ImageInputError(
            "ERROR: the current provider cannot serialize the attached image "
            "format; no provider request was sent."
        )
    try:
        from PIL import Image

        with Image.open(io.BytesIO(artifact.image_bytes)) as opened:
            opened.load()
            output = io.BytesIO()
            opened.save(output, format="PNG", optimize=False)
            canonical = output.getvalue()
    except Exception as exc:
        raise ImageInputError(
            "ERROR: the attached image could not be normalized safely; no "
            "provider request was sent."
        ) from exc
    if len(canonical) > wire_limit:
        raise ImageInputError(
            "ERROR: the normalized image exceeds the provider size limit; "
            "no provider request was sent."
        )
    return create_image_artifact(
        canonical,
        "image/png",
        artifact.source_type,
        parent_image_id=artifact.image_id,
        created_at=artifact.created_at,
        hwnd=artifact.hwnd,
        surface_id=artifact.surface_id,
        generation=artifact.generation,
        application_identity=artifact.application_identity,
        bounds=artifact.bounds,
        dpi=artifact.dpi,
        monitor=artifact.monitor,
        freshness=artifact.freshness,
        source_dimensions=(
            artifact.source_dimensions or (artifact.width, artifact.height)
        ),
        provider_dimensions=(artifact.width, artifact.height),
        detail_level=artifact.detail_level,
        crop_transform=artifact.crop_transform,
        lifecycle=artifact.lifecycle,
        expires_at=artifact.expires_at,
        privacy_class=artifact.privacy_class,
        metadata_stripped=True,
        capture_method=artifact.capture_method,
        source_pixel_format=artifact.source_pixel_format,
        pixel_format_conversion=artifact.pixel_format_conversion,
        alpha_handling=artifact.alpha_handling,
        source_color_space=artifact.source_color_space,
        color_space_conversion=artifact.color_space_conversion,
        capture_attempts=artifact.capture_attempts,
        fallback_used=artifact.fallback_used,
    )


def provider_image_policy(provider: str) -> ProviderImagePolicy:
    return PROVIDER_IMAGE_POLICIES.get(
        str(provider or "").lower(), ProviderImagePolicy(),
    )


def _enforce_provider_image_policy(
    artifacts: Iterable[ImageArtifact],
    provider: str,
) -> tuple[ImageArtifact, ...]:
    wires = tuple(
        _normalized_artifact_for_provider(artifact, provider)
        for artifact in artifacts
    )
    policy = provider_image_policy(provider)
    if policy.maximum_images is not None and len(wires) > policy.maximum_images:
        raise ImageInputError(
            "ERROR: too many images were supplied for the active provider; "
            "no provider request was sent.",
            reason_code="IMAGE_COUNT_LIMIT_EXCEEDED",
        )
    if policy.maximum_image_bytes is not None and any(
        len(item.image_bytes) > policy.maximum_image_bytes for item in wires
    ):
        raise ImageInputError(
            "ERROR: an image exceeds the active provider size limit; no "
            "provider request was sent.",
            reason_code="IMAGE_SIZE_LIMIT_EXCEEDED",
        )
    encoded_total = sum(
        4 * ((len(item.image_bytes) + 2) // 3)
        + len(f"data:{item.mime_type};base64,")
        for item in wires
    )
    if (
        policy.maximum_encoded_payload_bytes is not None
        and encoded_total > policy.maximum_encoded_payload_bytes
    ):
        raise ImageInputError(
            "ERROR: the encoded images exceed the active provider payload "
            "limit; no provider request was sent.",
            reason_code="IMAGE_ENCODED_LIMIT_EXCEEDED",
        )
    return wires


def artifact_data_url(artifact: ImageArtifact, provider: str = "") -> str:
    wire = (
        _normalized_artifact_for_provider(artifact, provider)
        if provider else artifact
    )
    return (
        f"data:{wire.mime_type};base64,"
        + base64.b64encode(wire.image_bytes).decode("ascii")
    )


def _wire_image_part(artifact: ImageArtifact, provider: str) -> dict:
    wire = _normalized_artifact_for_provider(artifact, provider)
    url = artifact_data_url(wire)
    if str(provider or "").lower() == "mistral":
        return {"type": "image_url", "image_url": url}
    image_url = {"url": url}
    if wire.detail_level in {"low", "high", "auto"}:
        image_url["detail"] = wire.detail_level
    return {"type": "image_url", "image_url": image_url}


def adapt_messages_for_provider(
    messages: list,
    provider: str,
    *,
    owner_id: str = "",
    turn_id: str = "",
) -> list:
    """Resolve opaque artifacts and serialize provider-specific image chunks."""

    provider = str(provider or "").lower()
    changed = False
    wire = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            wire.append(message)
            continue
        parts = []
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "image_ref":
                media_reference = dict(part)
                part = _wire_image_part(
                    resolve_image_reference(
                        media_reference,
                        owner_id=reference_owner_id(
                            media_reference,
                            conversation_id=owner_id,
                            turn_id=turn_id,
                        ),
                    ),
                    provider,
                )
                if provider == "chatgpt_account":
                    part["_osia_media"] = media_reference
                changed = True
            elif isinstance(part, dict) and part.get("type") == "image_url":
                decoded = decode_image_data_url(part.get("image_url"))
                media_reference = part.get("_osia_media")
                if provider == "chatgpt_account" and isinstance(
                    media_reference, dict,
                ):
                    artifact = resolve_image_reference(
                        media_reference,
                        owner_id=reference_owner_id(
                            media_reference,
                            conversation_id=owner_id,
                            turn_id=turn_id,
                        ),
                    )
                    if (
                        decoded.mime != artifact.mime_type
                        or decoded.content_hash != artifact.content_hash
                        or (decoded.width, decoded.height)
                        != (artifact.width, artifact.height)
                    ):
                        raise ImageInputError(
                            "ERROR: canonical image metadata does not match the "
                            "provider payload.",
                            reason_code="IMAGE_REF_METADATA_MISMATCH",
                        )
                    media_reference = dict(media_reference)
                else:
                    artifact = create_image_artifact(
                        decoded.data,
                        decoded.mime,
                        ImageSourceType.USER_UPLOAD,
                    )
                    media_reference = None
                    if provider == "chatgpt_account" and owner_id:
                        media_reference = register_image_artifact(
                            artifact,
                            owner_id=owner_id,
                        )
                part = _wire_image_part(artifact, provider)
                if provider == "chatgpt_account" and isinstance(
                    media_reference, dict,
                ):
                    part["_osia_media"] = media_reference
                changed = True
            parts.append(part)
        copied = dict(message)
        copied["content"] = parts
        wire.append(copied)
    result = wire if changed else messages
    serialized = []
    for message in result:
        if not isinstance(message, dict) or not isinstance(
            message.get("content"), list,
        ):
            continue
        for part in message["content"]:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            decoded = decode_image_data_url(part.get("image_url"))
            serialized.append(create_image_artifact(
                decoded.data,
                decoded.mime,
                ImageSourceType.USER_UPLOAD,
            ))
    _enforce_provider_image_policy(serialized, provider)
    return result


def content_with_images(
    text: str,
    artifacts: Iterable[ImageArtifact],
    *,
    owner_id: str = "",
) -> list[dict]:
    """Build provider-neutral multimodal content from canonical artifacts."""

    content: list[dict] = []
    if str(text or "").strip():
        content.append({"type": "text", "text": str(text).strip()})
    for artifact in artifacts:
        content.append(register_image_artifact(
            artifact,
            owner_id=owner_id,
        ))
    return content
