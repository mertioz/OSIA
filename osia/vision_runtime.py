"""Canonical perception/action runtime for every OSIA visual surface.

The model is an evidence source only.  This module owns image capability
preflight, bounded transport, strict verdict parsing, crop transforms, stale
frame rejection, and the no-click-on-ambiguity rule.  It never promotes a
model verdict to a terminal execution receipt. Desktop, Browser, screenshot
compatibility tools, visual action proposals, and post-action reconciliation
all converge here; platform drivers only supply exact target/capture primitives.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import secrets
import sys
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol


_MAX_IMAGE_DIMENSION = 8_192
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_OBSERVED_STATE_CHARS = 800
_MAX_AMBIGUITY_CHARS = 320
_MIN_CLICK_CONFIDENCE = 0.65


class VisualStatus(str, Enum):
    MATCH = "match"
    NONMATCH = "nonmatch"
    AMBIGUOUS = "ambiguous"
    CAPTURE_INVALID = "capture_invalid"
    TARGET_NOT_VISIBLE = "target_not_visible"
    STALE_FRAME = "stale_frame"
    UNSUPPORTED = "unsupported"
    PROVIDER_REJECTED = "provider_rejected"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    # Compatibility value emitted by the pre-V4 facade. New runtime receipts
    # use STALE_FRAME, but parsing legacy fixtures remains deterministic.
    STALE = "stale"


class VisionDomain(str, Enum):
    DESKTOP = "desktop"
    BROWSER = "browser"
    GLOBAL = "global"
    SYNTHETIC = "synthetic"


class FrameFreshness(str, Enum):
    FRESH = "fresh"
    EXPIRED = "expired"
    TARGET_STALE = "target_stale"
    GEOMETRY_CHANGED = "geometry_changed"
    INVALID = "invalid"


class VisionStage(str, Enum):
    TARGET_RESOLVED = "TARGET_RESOLVED"
    CAPTURE_REQUESTED = "CAPTURE_REQUESTED"
    CAPTURE_COMPLETED = "CAPTURE_COMPLETED"
    IMAGE_PART_BUILT = "IMAGE_PART_BUILT"
    IMAGE_INPUT_SENT = "IMAGE_INPUT_SENT"
    MODEL_RESPONSE_RECEIVED = "MODEL_RESPONSE_RECEIVED"
    VISUAL_VERDICT_PARSED = "VISUAL_VERDICT_PARSED"
    REFINE_REQUESTED = "REFINE_REQUESTED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    PREFLIGHT_PASSED = "PREFLIGHT_PASSED"
    DISPATCH_STARTED = "DISPATCH_STARTED"
    POST_CAPTURE_COMPLETED = "POST_CAPTURE_COMPLETED"
    RECONCILED = "RECONCILED"


class VisionFailure(str, Enum):
    """Canonical terminal reasons for one live Vision operation."""

    TARGET_RESOLUTION_FAILED = "TARGET_RESOLUTION_FAILED"
    TARGET_STALE = "TARGET_STALE"
    CAPTURE_PROTECTED = "CAPTURE_PROTECTED"
    CAPTURE_ACCESS_DENIED = "CAPTURE_ACCESS_DENIED"
    CAPTURE_UNSUPPORTED = "CAPTURE_UNSUPPORTED"
    CAPTURE_EMPTY = "CAPTURE_EMPTY"
    CAPTURE_DIMENSION_MISMATCH = "CAPTURE_DIMENSION_MISMATCH"
    CAPTURE_DEVICE_FAILED = "CAPTURE_DEVICE_FAILED"
    IMAGE_PART_BUILD_FAILED = "IMAGE_PART_BUILD_FAILED"
    IMAGE_PART_EMPTY = "IMAGE_PART_EMPTY"
    PROVIDER_REQUEST_FAILED = "PROVIDER_REQUEST_FAILED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_REJECTED_IMAGE = "PROVIDER_REJECTED_IMAGE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    VERDICT_TARGET_MISMATCH = "VERDICT_TARGET_MISMATCH"
    VERDICT_AMBIGUOUS = "VERDICT_AMBIGUOUS"


class CaptureFailure(str, Enum):
    UNSUPPORTED = "unsupported_capture"
    ACCESS_DENIED = "access_denied"
    PROTECTED_CONTENT = "protected_content"
    MINIMIZED_TARGET = "minimized_target"
    STALE_TARGET = "stale_target"
    GENERATION_MISMATCH = "generation_mismatch"
    RESIZED_DURING_CAPTURE = "resize_during_capture"
    EMPTY_FRAME = "empty_frame"
    BLACK_FRAME = "black_frame"
    DEVICE_LOSS = "device_loss"
    WRONG_DIMENSIONS = "wrong_dimensions"


def _capture_failure_class(failure: CaptureFailure) -> VisionFailure:
    return {
        CaptureFailure.ACCESS_DENIED: VisionFailure.CAPTURE_ACCESS_DENIED,
        CaptureFailure.PROTECTED_CONTENT: VisionFailure.CAPTURE_PROTECTED,
        CaptureFailure.MINIMIZED_TARGET: VisionFailure.TARGET_STALE,
        CaptureFailure.STALE_TARGET: VisionFailure.TARGET_STALE,
        CaptureFailure.GENERATION_MISMATCH: VisionFailure.TARGET_STALE,
        CaptureFailure.RESIZED_DURING_CAPTURE: VisionFailure.TARGET_STALE,
        CaptureFailure.EMPTY_FRAME: VisionFailure.CAPTURE_EMPTY,
        CaptureFailure.BLACK_FRAME: VisionFailure.CAPTURE_EMPTY,
        CaptureFailure.DEVICE_LOSS: VisionFailure.CAPTURE_DEVICE_FAILED,
        CaptureFailure.WRONG_DIMENSIONS: (
            VisionFailure.CAPTURE_DIMENSION_MISMATCH
        ),
        CaptureFailure.UNSUPPORTED: VisionFailure.CAPTURE_UNSUPPORTED,
    }[failure]


class VisualReconciliationStatus(str, Enum):
    EFFECT_VERIFIED = "effect_verified"
    NOT_APPLIED_VERIFIED = "not_applied_verified"
    EFFECT_POSSIBLE_RECONCILING = "effect_possible_reconciling"
    OBSERVER_FAILED = "observer_failed"
    TARGET_STALE = "target_stale"


@dataclass(frozen=True)
class StageReceipt:
    stage: VisionStage
    occurred_at: float
    reason_code: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "occurred_at": round(float(self.occurred_at), 6),
            "reason_code": _bounded_text(self.reason_code, 120),
            "metadata": _privacy_safe_metadata(self.metadata),
        }


@dataclass
class VisionTrace:
    operation_id: str = field(
        default_factory=lambda: "vop_" + secrets.token_urlsafe(15)
    )
    receipts: list[StageReceipt] = field(default_factory=list)
    failed_stage: VisionStage | None = None
    failure_class: VisionFailure | None = None
    failure_reason: str = ""

    def record(
        self,
        stage: VisionStage,
        reason_code: str,
        **metadata: Any,
    ) -> StageReceipt:
        receipt = StageReceipt(stage, time.time(), reason_code, metadata)
        self.receipts.append(receipt)
        return receipt

    def fail(
        self,
        stage: VisionStage,
        failure: VisionFailure,
        reason_code: str,
    ) -> None:
        """Terminate the trace at the exact stage without fabricating success."""

        if self.failure_class is not None:
            return
        self.failed_stage = stage
        self.failure_class = failure
        self.failure_reason = _bounded_text(reason_code, 120)

    def safe_dict(self) -> dict:
        payload = {
            "operation_id": self.operation_id,
            "receipts": [item.safe_dict() for item in self.receipts],
            "terminal_stage": (
                self.failed_stage.value
                if self.failed_stage is not None else
                self.receipts[-1].stage.value if self.receipts else ""
            ),
        }
        if self.failure_class is not None:
            payload.update({
                "failure_class": self.failure_class.value,
                "failure_reason": self.failure_reason,
            })
        return payload


class VisionCaptureError(RuntimeError):
    def __init__(self, failure: CaptureFailure, reason_code: str = ""):
        self.failure = failure
        self.reason_code = reason_code or failure.value
        super().__init__(self.reason_code)


class VisionTargetError(RuntimeError):
    def __init__(self, reason_code: str = "target_resolution_failed"):
        self.failure = VisionFailure.TARGET_RESOLUTION_FAILED
        self.reason_code = _bounded_text(reason_code, 120)
        super().__init__(self.reason_code)


class VisionTransportError(RuntimeError):
    """Typed failure before a trustworthy visual verdict exists."""

    def __init__(
        self,
        status: VisualStatus,
        reason_code: str,
        *,
        failure: VisionFailure = VisionFailure.PROVIDER_REQUEST_FAILED,
        stage: VisionStage = VisionStage.IMAGE_INPUT_SENT,
    ):
        self.status = status
        self.reason_code = _bounded_text(reason_code, 120)
        self.failure = failure
        self.stage = stage
        super().__init__(self.reason_code)


class VisionModelOutputError(ValueError):
    """The provider answered, but not with the canonical verdict contract."""

    def __init__(self, reason_code: str):
        self.status = VisualStatus.MODEL_OUTPUT_INVALID
        self.reason_code = _bounded_text(reason_code, 120)
        self.failure = VisionFailure.MODEL_OUTPUT_INVALID
        self.stage = VisionStage.VISUAL_VERDICT_PARSED
        super().__init__(self.reason_code)


def _privacy_safe_metadata(value: object) -> object:
    """Return bounded diagnostics with no image/base64/private payload fields."""

    forbidden = {
        "base64", "bytes", "content", "data_url", "image", "image_bytes",
        "messages", "pixels", "prompt", "raw", "screenshot",
    }
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = str(key or "").strip().lower()
            if normalized in forbidden or any(
                token in normalized for token in ("base64", "screenshot_bytes")
            ):
                continue
            result[str(key)[:80]] = _privacy_safe_metadata(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_privacy_safe_metadata(item) for item in value[:32]]
    if isinstance(value, bytes):
        return {"byte_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


@dataclass(frozen=True)
class VisualTarget:
    """Exact semantic/native identity resolved before any pixel capture."""

    application_id: str
    domain: VisionDomain
    surface_id: str
    surface_generation: str
    bounds: tuple[int, int, int, int]
    visible: bool = True
    minimized: bool = False
    native_window_id: str = ""
    page_id: str = ""
    page_generation: int = 0
    frame_id: str = ""
    document_epoch: int = 0
    monitor_id: str = ""
    dpi_scale: float = 1.0
    resolved_at: float = field(default_factory=time.time)
    allow_global_context: bool = False

    def __post_init__(self) -> None:
        left, top, right, bottom = self.bounds
        if right <= left or bottom <= top:
            raise ValueError("invalid_visual_target_bounds")
        if self.minimized and self.visible:
            object.__setattr__(self, "visible", False)
        if self.domain is VisionDomain.BROWSER and not self.page_id:
            raise ValueError("browser_visual_target_requires_page_id")
        if self.domain is VisionDomain.GLOBAL and not self.allow_global_context:
            raise ValueError("global_capture_requires_explicit_exception")

    @property
    def identity_key(self) -> tuple:
        return (
            self.domain.value,
            self.application_id,
            self.surface_id,
            self.surface_generation,
            self.native_window_id,
            self.page_id,
            self.page_generation,
            self.frame_id,
            self.document_epoch,
        )

    def safe_dict(self) -> dict:
        return {
            "application": _bounded_text(self.application_id, 120),
            "domain": self.domain.value,
            "surface_id": _bounded_text(self.surface_id, 180),
            "surface_generation": _bounded_text(self.surface_generation, 180),
            "bounds": list(self.bounds),
            "visible": bool(self.visible),
            "minimized": bool(self.minimized),
            "native_window_id": _bounded_text(self.native_window_id, 80),
            "page_id": _bounded_text(self.page_id, 180),
            "page_generation": int(self.page_generation),
            "frame_id": _bounded_text(self.frame_id, 180),
            "document_epoch": int(self.document_epoch),
            "monitor_id": _bounded_text(self.monitor_id, 120),
            "dpi_scale": round(float(self.dpi_scale), 4),
            "resolved_at": round(float(self.resolved_at), 6),
            "global_context_exception": bool(self.allow_global_context),
        }


class TargetResolver(Protocol):
    def __call__(self, semantic_target: str) -> VisualTarget:
        ...


class CaptureDriver(Protocol):
    def __call__(self, target: VisualTarget) -> "VisualFrame":
        ...


@dataclass(frozen=True)
class NormalizedBBox:
    """A normalized rectangle with an exclusive right/bottom edge."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("non_finite_normalized_bbox")
        if not (
            0.0 <= self.left < self.right <= 1.0
            and 0.0 <= self.top < self.bottom <= 1.0
        ):
            raise ValueError("invalid_normalized_bbox")

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def to_dict(self) -> dict:
        return {
            "left": round(self.left, 6),
            "top": round(self.top, 6),
            "right": round(self.right, 6),
            "bottom": round(self.bottom, 6),
        }


@dataclass(frozen=True)
class CoordinateTransform:
    """Map a provider image through source/client to physical screen pixels."""

    source_width: int
    source_height: int
    source_box: tuple[float, float, float, float]
    screen_bounds: tuple[int, int, int, int]
    client_origin_screen: tuple[int, int]
    dpi_scale: float = 1.0
    monitor_id: str = ""
    window_bounds: tuple[int, int, int, int] | None = None
    client_bounds: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.source_width <= 0 or self.source_height <= 0:
            raise ValueError("invalid_source_dimensions")
        left, top, right, bottom = self.source_box
        if not (
            0.0 <= left < right <= float(self.source_width)
            and 0.0 <= top < bottom <= float(self.source_height)
        ):
            raise ValueError("invalid_source_box")
        screen_left, screen_top, screen_right, screen_bottom = self.screen_bounds
        if screen_right <= screen_left or screen_bottom <= screen_top:
            raise ValueError("invalid_screen_bounds")
        if not math.isfinite(self.dpi_scale) or self.dpi_scale <= 0:
            raise ValueError("invalid_dpi_scale")

    def bbox_source_pixels(
        self,
        bbox: NormalizedBBox,
    ) -> tuple[float, float, float, float]:
        left, top, right, bottom = self.source_box
        width = right - left
        height = bottom - top
        return (
            left + bbox.left * width,
            top + bbox.top * height,
            left + bbox.right * width,
            top + bbox.bottom * height,
        )

    def source_to_client(self, x: float, y: float) -> tuple[float, float]:
        screen_left, screen_top, screen_right, screen_bottom = self.screen_bounds
        sx = screen_left + (float(x) / self.source_width) * (
            screen_right - screen_left
        )
        sy = screen_top + (float(y) / self.source_height) * (
            screen_bottom - screen_top
        )
        origin_x, origin_y = self.client_origin_screen
        return sx - origin_x, sy - origin_y

    def client_to_physical_screen(self, x: float, y: float) -> tuple[int, int]:
        # The capturing thread is per-monitor-DPI-aware, so client and origin
        # values are already physical pixels. ``dpi_scale`` remains explicit
        # provenance and must not be applied a second time.
        origin_x, origin_y = self.client_origin_screen
        return int(round(origin_x + x)), int(round(origin_y + y))

    def source_to_screen(self, x: float, y: float) -> tuple[int, int]:
        client_x, client_y = self.source_to_client(x, y)
        return self.client_to_physical_screen(client_x, client_y)

    @property
    def source_to_screen_affine(self) -> tuple[float, float, float, float, float, float]:
        """Exact 2D affine transform ``(a,b,c,d,e,f)`` in physical pixels."""

        screen_left, screen_top, screen_right, screen_bottom = self.screen_bounds
        return (
            (screen_right - screen_left) / float(self.source_width),
            0.0,
            float(screen_left),
            0.0,
            (screen_bottom - screen_top) / float(self.source_height),
            float(screen_top),
        )

    def bbox_screen_pixels(
        self,
        bbox: NormalizedBBox,
    ) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.bbox_source_pixels(bbox)
        screen_left, screen_top = self.source_to_screen(left, top)
        screen_right, screen_bottom = self.source_to_screen(right, bottom)
        return screen_left, screen_top, screen_right, screen_bottom

    def bbox_client_pixels(
        self,
        bbox: NormalizedBBox,
    ) -> tuple[int, int, int, int]:
        left, top, right, bottom = self.bbox_screen_pixels(bbox)
        origin_x, origin_y = self.client_origin_screen
        return (
            left - origin_x,
            top - origin_y,
            right - origin_x,
            bottom - origin_y,
        )

    def safe_dict(self) -> dict:
        return {
            "source_dimensions": {
                "width": self.source_width,
                "height": self.source_height,
            },
            "source_box": [round(value, 3) for value in self.source_box],
            "screen_bounds": list(self.screen_bounds),
            "client_origin_screen": list(self.client_origin_screen),
            "dpi_scale": round(float(self.dpi_scale), 4),
            "monitor_id": _bounded_text(self.monitor_id, 120),
            "window_bounds": list(self.window_bounds or self.screen_bounds),
            "client_bounds": list(self.client_bounds or ()),
            "source_to_screen_affine": [
                round(value, 9) for value in self.source_to_screen_affine
            ],
            "mapping_chain": "crop_to_source_to_client_to_physical_screen",
        }


@dataclass(frozen=True, repr=False)
class VisualFrame:
    """One immutable exact-bound frame; bytes remain ephemeral and repr-safe."""

    capture_id: str
    provider: str
    model: str
    application_id: str
    surface_ref: str
    window_generation: str
    structural_epoch: int
    captured_at: float
    image_width: int
    image_height: int
    mime: str
    image_bytes: bytes = field(repr=False)
    image_sha256: str = ""
    transform: CoordinateTransform | None = None
    native_window_token: str = field(default="", repr=False)
    capture_method: str = ""
    exact_surface_pixels: bool = False
    foreground: bool = True
    preparation_latency_ms: float = 0.0
    domain: VisionDomain = VisionDomain.DESKTOP
    target_identity: tuple = ()
    page_id: str = ""
    page_generation: int = 0
    browser_frame_id: str = ""
    document_epoch: int = 0
    monitor_id: str = ""
    window_bounds: tuple[int, int, int, int] | None = None
    client_bounds: tuple[int, int, int, int] | None = None
    screen_origin: tuple[int, int] = (0, 0)
    freshness: FrameFreshness = FrameFreshness.FRESH
    expires_at: float = 0.0
    registered_image_id: str = ""
    parent_image_id: str = ""
    source_pixel_format: str = ""
    pixel_format_conversion: str = ""
    alpha_handling: str = ""
    source_color_space: str = ""
    color_space_conversion: str = ""
    capture_attempts: tuple[dict, ...] = ()
    fallback_used: bool = False

    def __post_init__(self) -> None:
        if self.mime != "image/png":
            raise ValueError("canonical_capture_requires_png")
        if not self.image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid_capture_png")
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("invalid_capture_dimensions")
        digest = hashlib.sha256(self.image_bytes).hexdigest()
        if self.image_sha256 and self.image_sha256 != digest:
            raise ValueError("capture_hash_mismatch")
        if not self.image_sha256:
            object.__setattr__(self, "image_sha256", digest)
        if not math.isfinite(float(self.captured_at)) or self.captured_at <= 0:
            raise ValueError("invalid_capture_timestamp")
        if self.expires_at <= 0:
            object.__setattr__(self, "expires_at", self.captured_at + 8.0)
        for field_name, maximum in (
            ("registered_image_id", 160),
            ("parent_image_id", 160),
            ("source_pixel_format", 80),
            ("pixel_format_conversion", 160),
            ("alpha_handling", 160),
            ("source_color_space", 120),
            ("color_space_conversion", 160),
        ):
            object.__setattr__(
                self,
                field_name,
                str(getattr(self, field_name) or "").strip()[:maximum],
            )
        if self.domain is VisionDomain.BROWSER and not self.page_id:
            raise ValueError("browser_visual_frame_requires_page_identity")

    @property
    def frame_id(self) -> str:
        return self.capture_id

    @property
    def surface_id(self) -> str:
        return self.surface_ref

    @property
    def surface_generation(self) -> str:
        return self.window_generation

    @property
    def content_reference(self) -> str:
        return "sha256:" + self.image_sha256

    def is_fresh(self, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else float(now)
        return self.freshness is FrameFreshness.FRESH and moment <= self.expires_at

    @property
    def identity_key(self) -> tuple:
        return (
            self.provider,
            self.model,
            self.surface_ref,
            self.structural_epoch,
            self.window_generation,
            self.native_window_token,
            self.page_id,
            self.page_generation,
            self.browser_frame_id,
            self.document_epoch,
        )

    @property
    def window_identity_key(self) -> tuple:
        return (
            self.domain.value,
            self.provider,
            self.model,
            self.application_id,
            self.window_generation,
            self.native_window_token,
            self.page_id,
            self.page_generation,
            self.browser_frame_id,
        )

    def safe_dict(self) -> dict:
        payload = {
            "capture_id": self.capture_id,
            "frame_id": self.frame_id,
            "provider": self.provider,
            "model": self.model,
            "application": self.application_id,
            "domain": self.domain.value,
            "surface_ref": self.surface_ref,
            "surface_id": self.surface_id,
            "window_generation": self.window_generation,
            "surface_generation": self.surface_generation,
            "structural_epoch": self.structural_epoch,
            "captured_at": round(self.captured_at, 3),
            "dimensions": {
                "width": self.image_width,
                "height": self.image_height,
            },
            "mime": self.mime,
            "image_sha256": self.image_sha256,
            "content_reference": self.content_reference,
            "capture_method": self.capture_method,
            "capture_driver": self.capture_method,
            "exact_surface_pixels": self.exact_surface_pixels,
            "foreground": self.foreground,
            "preparation_latency_ms": round(self.preparation_latency_ms, 3),
            "page_id": self.page_id,
            "page_generation": self.page_generation,
            "browser_frame_id": self.browser_frame_id,
            "document_epoch": self.document_epoch,
            "monitor_id": self.monitor_id,
            "window_bounds": list(self.window_bounds or ()),
            "client_bounds": list(self.client_bounds or ()),
            "screen_origin": list(self.screen_origin),
            "freshness": self.freshness.value,
            "expires_at": round(float(self.expires_at), 6),
            "registered_image_id": self.registered_image_id,
            "parent_image_id": self.parent_image_id,
            "source_pixel_format": self.source_pixel_format,
            "pixel_format_conversion": self.pixel_format_conversion,
            "alpha_handling": self.alpha_handling,
            "source_color_space": self.source_color_space,
            "color_space_conversion": self.color_space_conversion,
            "capture_attempts": [dict(item) for item in self.capture_attempts],
            "fallback_used": self.fallback_used,
            "crop_rectangle": (
                [round(value, 3) for value in self.transform.source_box]
                if self.transform is not None else []
            ),
        }
        if self.transform is not None:
            payload["coordinate_transform"] = self.transform.safe_dict()
        return payload

    def to_image_artifact(self, *, parent_image_id: str | None = None):
        """Project this mechanical frame into the canonical image abstraction."""

        from osia.image_inputs import ImageSourceType, create_image_artifact

        source_type = (
            ImageSourceType.REFINED_CROP
            if self.capture_method == "adaptive_crop" else
            ImageSourceType.BROWSER_CAPTURE
            if self.domain is VisionDomain.BROWSER else
            ImageSourceType.DESKTOP_CAPTURE
        )
        native_window_id = (
            str(self.target_identity[4])
            if len(self.target_identity) > 4 else ""
        )
        effective_parent = (
            self.parent_image_id
            if parent_image_id is None else str(parent_image_id or "")
        )
        source_dimensions = (
            (self.transform.source_width, self.transform.source_height)
            if self.transform is not None else
            (self.image_width, self.image_height)
        )
        artifact = create_image_artifact(
            self.image_bytes,
            self.mime,
            source_type,
            parent_image_id=effective_parent,
            created_at=self.captured_at,
            hwnd=native_window_id,
            surface_id=self.surface_ref,
            generation=self.window_generation,
            application_identity=self.application_id,
            bounds=self.window_bounds,
            dpi=(
                max(1, int(round(float(self.transform.dpi_scale) * 96.0)))
                if self.transform is not None else 0
            ),
            monitor=self.monitor_id,
            freshness=self.freshness.value,
            source_dimensions=source_dimensions,
            provider_dimensions=(self.image_width, self.image_height),
            detail_level="high",
            crop_transform=(
                self.transform.safe_dict() if self.transform is not None else None
            ),
            expires_at=self.expires_at,
            capture_method=self.capture_method,
            source_pixel_format=self.source_pixel_format,
            pixel_format_conversion=self.pixel_format_conversion,
            alpha_handling=self.alpha_handling,
            source_color_space=self.source_color_space,
            color_space_conversion=self.color_space_conversion,
            capture_attempts=self.capture_attempts,
            fallback_used=self.fallback_used,
        )
        if (
            self.registered_image_id
            and artifact.image_id != self.registered_image_id
        ):
            raise ValueError("capture_registered_artifact_mismatch")
        return artifact


# Compatibility name retained for WOR-157/258 callers. It is a direct alias,
# not a second contract or implementation.
BoundVisualCapture = VisualFrame


@dataclass(frozen=True)
class VisualVerdict:
    status: VisualStatus
    expected_postcondition: str
    observed_state: str
    confidence: float
    target_bbox: NormalizedBBox | None = None
    need_refine: bool = False
    ambiguity: str = ""
    pass_index: int = 0
    frame_id: str = ""
    target_identity: Mapping[str, Any] = field(default_factory=dict)
    normalized_regions: tuple[NormalizedBBox, ...] = ()
    refinement_roi: NormalizedBBox | None = None
    reason_code: str = ""

    @property
    def click_eligible(self) -> bool:
        return bool(
            self.status is VisualStatus.MATCH
            and self.target_bbox is not None
            and self.confidence >= _MIN_CLICK_CONFIDENCE
            and not self.need_refine
            and not self.ambiguity
        )

    def safe_dict(self) -> dict:
        payload = {
            "status": self.status.value,
            "expected_postcondition": self.expected_postcondition,
            "observed_state": self.observed_state,
            "confidence": round(self.confidence, 4),
            "need_refine": self.need_refine,
            "ambiguity": self.ambiguity,
            "pass_index": self.pass_index,
            "click_eligible": self.click_eligible,
            "frame_id": _bounded_text(self.frame_id, 180),
            "target_identity": _privacy_safe_metadata(self.target_identity),
            "normalized_regions": [
                item.to_dict() for item in self.normalized_regions[:16]
            ],
            "reason_code": _bounded_text(self.reason_code, 120),
        }
        if self.target_bbox is not None:
            payload["target_bbox"] = self.target_bbox.to_dict()
        if self.refinement_roi is not None:
            payload["refinement_roi"] = self.refinement_roi.to_dict()
        return payload


@dataclass(frozen=True)
class VisualAnalysis:
    capture: BoundVisualCapture
    verdict: VisualVerdict
    diagnostics: dict
    passes: tuple[dict, ...] = ()
    trace: Mapping[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict:
        return {
            "capture": self.capture.safe_dict(),
            "verdict": self.verdict.safe_dict(),
            "diagnostics": dict(self.diagnostics),
            "passes": [dict(item) for item in self.passes],
            "trace": _privacy_safe_metadata(self.trace),
        }


@dataclass(frozen=True)
class VisualActionProposal:
    proposal_id: str
    source_frame_id: str
    target_identity: tuple
    semantic_target: str
    action_type: str
    normalized_bbox: NormalizedBBox
    confidence: float
    ambiguous: bool
    expected_postcondition: str
    required_foreground: bool
    safe_interior_bbox: NormalizedBBox
    expires_at: float
    source_geometry: tuple = ()
    source_transform: CoordinateTransform | None = field(
        default=None, repr=False,
    )
    structured_actuator: str = ""

    def safe_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "source_frame_id": self.source_frame_id,
            "target_identity_ref": hashlib.sha256(
                repr(self.target_identity).encode("utf-8", errors="replace")
            ).hexdigest(),
            "semantic_target": _bounded_text(self.semantic_target, 500),
            "action_type": _bounded_text(self.action_type, 80),
            "normalized_bbox": self.normalized_bbox.to_dict(),
            "confidence": round(float(self.confidence), 4),
            "ambiguous": bool(self.ambiguous),
            "expected_postcondition": _bounded_text(
                self.expected_postcondition, 500
            ),
            "required_foreground": bool(self.required_foreground),
            "safe_interior_bbox": self.safe_interior_bbox.to_dict(),
            "expires_at": round(float(self.expires_at), 6),
            "source_geometry": _privacy_safe_metadata(self.source_geometry),
            "coordinate_transform": (
                self.source_transform.safe_dict()
                if self.source_transform is not None else {}
            ),
            "structured_actuator": _bounded_text(self.structured_actuator, 120),
        }


@dataclass(frozen=True)
class VisualPreflight:
    passed: bool
    reason_code: str
    point: tuple[int, int] | None = None
    reobserve_required: bool = False

    def safe_dict(self) -> dict:
        return {
            "passed": bool(self.passed),
            "reason_code": _bounded_text(self.reason_code, 120),
            "point": list(self.point) if self.point is not None else None,
            "reobserve_required": bool(self.reobserve_required),
        }


@dataclass(frozen=True)
class VisualReconciliation:
    status: VisualReconciliationStatus
    reason_code: str
    pre_frame_id: str
    post_frame_id: str
    verdict: VisualVerdict | None = None

    def safe_dict(self) -> dict:
        payload = {
            "status": self.status.value,
            "reason_code": _bounded_text(self.reason_code, 120),
            "pre_frame_id": _bounded_text(self.pre_frame_id, 180),
            "post_frame_id": _bounded_text(self.post_frame_id, 180),
        }
        if self.verdict is not None:
            payload["verdict"] = self.verdict.safe_dict()
        return payload


@dataclass(frozen=True, repr=False)
class ImageRequest:
    provider: str
    model: str
    frame_id: str
    target_binding: str
    mime: str
    dimensions: tuple[int, int]
    messages: list[dict] = field(repr=False)
    schema: dict = field(repr=False)
    image_part_count: int = 1

    def safe_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "frame_id": self.frame_id,
            "target_binding": self.target_binding,
            "mime": self.mime,
            "dimensions": list(self.dimensions),
            "image_part_count": self.image_part_count,
            "image_part_constructed": self.image_part_count == 1,
        }


def _bounded_text(value: object, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _frame_geometry_identity(frame: VisualFrame) -> tuple:
    transform = frame.transform
    return (
        tuple(frame.window_bounds or ()),
        tuple(frame.client_bounds or ()),
        tuple(frame.screen_origin),
        frame.monitor_id,
        (
            (
                tuple(transform.screen_bounds),
                tuple(transform.client_origin_screen),
                round(float(transform.dpi_scale), 6),
            )
            if transform is not None else ()
        ),
    )


def _encode_png(image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG", compress_level=6)
    return output.getvalue()


def prepare_capture(
    png_bytes: bytes,
    *,
    provider: str,
    model: str,
    application_id: str,
    surface_ref: str,
    window_generation: str,
    structural_epoch: int,
    transform: CoordinateTransform,
    native_window_token: str = "",
    capture_method: str,
    exact_surface_pixels: bool,
    foreground: bool = True,
    minimum_short_side: int = 0,
    domain: VisionDomain | str = VisionDomain.DESKTOP,
    target_identity: tuple = (),
    page_id: str = "",
    page_generation: int = 0,
    browser_frame_id: str = "",
    document_epoch: int = 0,
    monitor_id: str = "",
    window_bounds: tuple[int, int, int, int] | None = None,
    client_bounds: tuple[int, int, int, int] | None = None,
    screen_origin: tuple[int, int] = (0, 0),
    freshness: FrameFreshness | str = FrameFreshness.FRESH,
    expires_at: float = 0.0,
    captured_at: float | None = None,
    registered_image_id: str = "",
    parent_image_id: str = "",
    source_pixel_format: str = "",
    pixel_format_conversion: str = "",
    alpha_handling: str = "",
    source_color_space: str = "",
    color_space_conversion: str = "",
    capture_attempts: Iterable[dict] = (),
    fallback_used: bool = False,
) -> VisualFrame:
    """Canonicalize and adapt a PNG without collapsing normal desktop detail."""

    from PIL import Image

    started = time.perf_counter()
    capture_timestamp = (
        time.time() if captured_at is None else float(captured_at)
    )
    if not math.isfinite(capture_timestamp) or capture_timestamp <= 0:
        raise ValueError("invalid_capture_timestamp")
    with Image.open(io.BytesIO(png_bytes)) as opened:
        if opened.format != "PNG":
            raise ValueError("canonical_capture_requires_png")
        width, height = opened.size
    if width <= 0 or height <= 0:
        raise ValueError("empty_capture")

    max_scale = min(
        1.0,
        _MAX_IMAGE_DIMENSION / max(width, height),
        math.sqrt(_MAX_IMAGE_PIXELS / float(width * height)),
        (
            math.sqrt(_MAX_IMAGE_BYTES / float(len(png_bytes))) * 0.96
            if len(png_bytes) > _MAX_IMAGE_BYTES else 1.0
        ),
    )
    upscale = 1.0
    if (
        max_scale >= 1.0
        and minimum_short_side > 0
        and min(width, height) < minimum_short_side
    ):
        upscale = min(
            minimum_short_side / min(width, height),
            _MAX_IMAGE_DIMENSION / max(width, height),
            math.sqrt(_MAX_IMAGE_PIXELS / float(width * height)),
        )

    if max_scale >= 1.0 and upscale <= 1.0:
        # Every caller supplies an OSIA-generated screenshot (ImageGrab,
        # PrintWindow, Playwright, or a crop produced above), so preserving an
        # already bounded PNG avoids a lossy/expensive arbitrary normalization.
        canonical = bytes(png_bytes)
        output_width, output_height = width, height
    else:
        with Image.open(io.BytesIO(png_bytes)) as opened:
            opened.load()
            image = opened.convert("RGB")
        scale = max_scale if max_scale < 1.0 else upscale
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize(
            (
                max(1, int(math.floor(width * scale))),
                max(1, int(math.floor(height * scale))),
            ),
            resampling,
        )
        canonical = _encode_png(image)
        while len(canonical) > _MAX_IMAGE_BYTES and min(image.size) > 256:
            byte_scale = min(
                0.90,
                math.sqrt(_MAX_IMAGE_BYTES / float(len(canonical))) * 0.96,
            )
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image = image.resize(
                (
                    max(1, int(math.floor(image.width * byte_scale))),
                    max(1, int(math.floor(image.height * byte_scale))),
                ),
                resampling,
            )
            canonical = _encode_png(image)
        output_width, output_height = image.size
    if len(canonical) > _MAX_IMAGE_BYTES:
        raise ValueError("capture_exceeds_image_budget")

    preparation_ms = (time.perf_counter() - started) * 1000.0
    return VisualFrame(
        capture_id="vcap_" + secrets.token_urlsafe(15),
        provider=str(provider or "").lower(),
        model=str(model or ""),
        application_id=_bounded_text(application_id, 120),
        surface_ref=_bounded_text(surface_ref, 160),
        window_generation=_bounded_text(window_generation, 160),
        structural_epoch=max(0, int(structural_epoch)),
        captured_at=capture_timestamp,
        image_width=output_width,
        image_height=output_height,
        mime="image/png",
        image_bytes=canonical,
        transform=transform,
        native_window_token=str(native_window_token or ""),
        capture_method=_bounded_text(capture_method, 80),
        exact_surface_pixels=bool(exact_surface_pixels),
        foreground=bool(foreground),
        preparation_latency_ms=preparation_ms,
        domain=(domain if isinstance(domain, VisionDomain) else VisionDomain(domain)),
        target_identity=tuple(target_identity or ()),
        page_id=_bounded_text(page_id, 180),
        page_generation=max(0, int(page_generation)),
        browser_frame_id=_bounded_text(browser_frame_id, 180),
        document_epoch=max(0, int(document_epoch)),
        monitor_id=_bounded_text(monitor_id, 120),
        window_bounds=window_bounds,
        client_bounds=client_bounds,
        screen_origin=(int(screen_origin[0]), int(screen_origin[1])),
        freshness=(
            freshness
            if isinstance(freshness, FrameFreshness)
            else FrameFreshness(freshness)
        ),
        expires_at=float(expires_at or 0.0),
        registered_image_id=_bounded_text(registered_image_id, 160),
        parent_image_id=_bounded_text(parent_image_id, 160),
        source_pixel_format=_bounded_text(source_pixel_format, 80),
        pixel_format_conversion=_bounded_text(
            pixel_format_conversion, 160,
        ),
        alpha_handling=_bounded_text(alpha_handling, 160),
        source_color_space=_bounded_text(source_color_space, 120),
        color_space_conversion=_bounded_text(
            color_space_conversion, 160,
        ),
        capture_attempts=tuple(
            dict(item) for item in capture_attempts
            if isinstance(item, dict)
        )[:8],
        fallback_used=bool(fallback_used),
    )


def crop_capture(
    capture: VisualFrame,
    bbox: NormalizedBBox,
    *,
    padding_ratio: float = 0.35,
) -> VisualFrame:
    """Create an adaptive zoom crop while preserving the source transform."""

    if capture.transform is None:
        raise ValueError("capture_transform_required")
    from PIL import Image

    transform = capture.transform
    target_left, target_top, target_right, target_bottom = (
        transform.bbox_source_pixels(bbox)
    )
    target_width = target_right - target_left
    target_height = target_bottom - target_top
    source_left, source_top, source_right, source_bottom = transform.source_box
    minimum_span = min(
        max(source_right - source_left, source_bottom - source_top),
        256.0,
    )
    span_x = max(target_width * (1.0 + 2.0 * padding_ratio), minimum_span)
    span_y = max(target_height * (1.0 + 2.0 * padding_ratio), minimum_span)
    center_x = (target_left + target_right) / 2.0
    center_y = (target_top + target_bottom) / 2.0
    crop_left = max(source_left, center_x - span_x / 2.0)
    crop_top = max(source_top, center_y - span_y / 2.0)
    crop_right = min(source_right, center_x + span_x / 2.0)
    crop_bottom = min(source_bottom, center_y + span_y / 2.0)
    if crop_right - crop_left < span_x:
        if crop_left <= source_left:
            crop_right = min(source_right, source_left + span_x)
        else:
            crop_left = max(source_left, source_right - span_x)
    if crop_bottom - crop_top < span_y:
        if crop_top <= source_top:
            crop_bottom = min(source_bottom, source_top + span_y)
        else:
            crop_top = max(source_top, source_bottom - span_y)

    represented_width = source_right - source_left
    represented_height = source_bottom - source_top
    with Image.open(io.BytesIO(capture.image_bytes)) as opened:
        opened.load()
        image = opened.convert("RGB")
    pixel_box = (
        max(0, int(math.floor(
            (crop_left - source_left) / represented_width * image.width
        ))),
        max(0, int(math.floor(
            (crop_top - source_top) / represented_height * image.height
        ))),
        min(image.width, int(math.ceil(
            (crop_right - source_left) / represented_width * image.width
        ))),
        min(image.height, int(math.ceil(
            (crop_bottom - source_top) / represented_height * image.height
        ))),
    )
    if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
        raise ValueError("empty_refinement_crop")
    cropped_png = _encode_png(image.crop(pixel_box))
    actual_source_box = (
        source_left + pixel_box[0] / image.width * represented_width,
        source_top + pixel_box[1] / image.height * represented_height,
        source_left + pixel_box[2] / image.width * represented_width,
        source_top + pixel_box[3] / image.height * represented_height,
    )
    if all(
        math.isclose(actual, original, rel_tol=0.0, abs_tol=1e-6)
        for actual, original in zip(actual_source_box, transform.source_box)
    ):
        return capture
    refined_transform = replace(
        transform,
        source_box=actual_source_box,
    )
    source_artifact_id = (
        capture.registered_image_id
        or capture.to_image_artifact().image_id
    )
    return prepare_capture(
        cropped_png,
        provider=capture.provider,
        model=capture.model,
        application_id=capture.application_id,
        surface_ref=capture.surface_ref,
        window_generation=capture.window_generation,
        structural_epoch=capture.structural_epoch,
        transform=refined_transform,
        native_window_token=capture.native_window_token,
        capture_method="adaptive_crop",
        exact_surface_pixels=capture.exact_surface_pixels,
        foreground=capture.foreground,
        minimum_short_side=768,
        domain=capture.domain,
        target_identity=capture.target_identity,
        page_id=capture.page_id,
        page_generation=capture.page_generation,
        browser_frame_id=capture.browser_frame_id,
        document_epoch=capture.document_epoch,
        monitor_id=capture.monitor_id,
        window_bounds=capture.window_bounds,
        client_bounds=capture.client_bounds,
        screen_origin=capture.screen_origin,
        freshness=capture.freshness,
        expires_at=capture.expires_at,
        captured_at=capture.captured_at,
        parent_image_id=source_artifact_id,
        source_pixel_format=capture.source_pixel_format,
        pixel_format_conversion=capture.pixel_format_conversion,
        alpha_handling=capture.alpha_handling,
        source_color_space=capture.source_color_space,
        color_space_conversion=capture.color_space_conversion,
        capture_attempts=capture.capture_attempts,
        fallback_used=capture.fallback_used,
    )


def visual_frame_pixels_unchanged(
    source: VisualFrame,
    fresh: VisualFrame,
    bbox: NormalizedBBox | None = None,
) -> bool:
    """Return whether a fresh capture still contains the selected source pixels.

    The source frame may be a resized refinement crop. Reproject the model's
    selected bbox (or the complete frame when omitted) into the fresh frame,
    resize that fresh region exactly as the source was prepared, and compare
    decoded RGB pixels. Any unavailable or ambiguous transform fails closed.
    """

    if source.transform is None or fresh.transform is None:
        return False
    if (
        source.domain is not fresh.domain
        or source.provider != fresh.provider
        or source.model != fresh.model
        or source.application_id != fresh.application_id
        or source.window_generation != fresh.window_generation
        or source.native_window_token != fresh.native_window_token
    ):
        return False

    source_transform = source.transform
    fresh_transform = fresh.transform
    fresh_screen_left, fresh_screen_top, fresh_screen_right, fresh_screen_bottom = (
        fresh_transform.screen_bounds
    )
    fresh_screen_width = fresh_screen_right - fresh_screen_left
    fresh_screen_height = fresh_screen_bottom - fresh_screen_top
    if fresh_screen_width <= 0 or fresh_screen_height <= 0:
        return False

    source_screen_left, source_screen_top, source_screen_right, source_screen_bottom = (
        source_transform.screen_bounds
    )
    source_screen_width = source_screen_right - source_screen_left
    source_screen_height = source_screen_bottom - source_screen_top
    frame_left, frame_top, frame_right, frame_bottom = (
        source_transform.source_box
    )
    source_left, source_top, source_right, source_bottom = (
        source_transform.bbox_source_pixels(bbox)
        if bbox is not None else source_transform.source_box
    )
    physical_box = (
        source_screen_left
        + source_left / source_transform.source_width * source_screen_width,
        source_screen_top
        + source_top / source_transform.source_height * source_screen_height,
        source_screen_left
        + source_right / source_transform.source_width * source_screen_width,
        source_screen_top
        + source_bottom / source_transform.source_height * source_screen_height,
    )
    if (
        physical_box[0] < fresh_screen_left - 0.5
        or physical_box[1] < fresh_screen_top - 0.5
        or physical_box[2] > fresh_screen_right + 0.5
        or physical_box[3] > fresh_screen_bottom + 0.5
    ):
        return False

    fresh_source_box = tuple(
        (
            (physical_box[index] - fresh_transform.screen_bounds[index % 2])
            / float(fresh_screen_width if index % 2 == 0 else fresh_screen_height)
            * (
                fresh_transform.source_width
                if index % 2 == 0 else fresh_transform.source_height
            )
        )
        for index in range(4)
    )
    fresh_left, fresh_top, fresh_right, fresh_bottom = fresh_transform.source_box
    represented_width = fresh_right - fresh_left
    represented_height = fresh_bottom - fresh_top
    if (
        represented_width <= 0
        or represented_height <= 0
        or fresh_source_box[0] < fresh_left - 0.5
        or fresh_source_box[1] < fresh_top - 0.5
        or fresh_source_box[2] > fresh_right + 0.5
        or fresh_source_box[3] > fresh_bottom + 0.5
    ):
        return False

    from PIL import Image, ImageChops

    try:
        with Image.open(io.BytesIO(source.image_bytes)) as opened:
            source_image = opened.convert("RGB")
        with Image.open(io.BytesIO(fresh.image_bytes)) as opened:
            fresh_image = opened.convert("RGB")
    except Exception:
        return False

    source_represented_width = frame_right - frame_left
    source_represented_height = frame_bottom - frame_top
    if source_represented_width <= 0 or source_represented_height <= 0:
        return False
    source_pixel_box = (
        max(0, int(math.floor(
            (source_left - frame_left)
            / source_represented_width * source_image.width
        ))),
        max(0, int(math.floor(
            (source_top - frame_top)
            / source_represented_height * source_image.height
        ))),
        min(source_image.width, int(math.ceil(
            (source_right - frame_left)
            / source_represented_width * source_image.width
        ))),
        min(source_image.height, int(math.ceil(
            (source_bottom - frame_top)
            / source_represented_height * source_image.height
        ))),
    )
    if (
        source_pixel_box[2] <= source_pixel_box[0]
        or source_pixel_box[3] <= source_pixel_box[1]
    ):
        return False
    source_region = source_image.crop(source_pixel_box)

    pixel_box = (
        max(0, int(math.floor(
            (fresh_source_box[0] - fresh_left)
            / represented_width * fresh_image.width
        ))),
        max(0, int(math.floor(
            (fresh_source_box[1] - fresh_top)
            / represented_height * fresh_image.height
        ))),
        min(fresh_image.width, int(math.ceil(
            (fresh_source_box[2] - fresh_left)
            / represented_width * fresh_image.width
        ))),
        min(fresh_image.height, int(math.ceil(
            (fresh_source_box[3] - fresh_top)
            / represented_height * fresh_image.height
        ))),
    )
    if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
        return False
    fresh_region = fresh_image.crop(pixel_box)
    if fresh_region.size != source_region.size:
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        fresh_region = fresh_region.resize(source_region.size, resampling)
    return ImageChops.difference(source_region, fresh_region).getbbox() is None


def _verdict_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                # Transport, staleness, unsupported paths, and invalid model
                # output are assigned locally. A model looking only at pixels
                # may assert only evidence statuses.
                "enum": [
                    VisualStatus.MATCH.value,
                    VisualStatus.NONMATCH.value,
                    VisualStatus.AMBIGUOUS.value,
                    VisualStatus.CAPTURE_INVALID.value,
                    VisualStatus.TARGET_NOT_VISIBLE.value,
                ],
            },
            "expected_postcondition": {"type": "string"},
            "observed_state": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "target_bbox": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "left": {"type": "number"},
                            "top": {"type": "number"},
                            "right": {"type": "number"},
                            "bottom": {"type": "number"},
                        },
                        "required": ["left", "top", "right", "bottom"],
                    },
                ],
            },
            "need_refine": {"type": "boolean"},
            "ambiguity": {"type": "string"},
        },
        "required": [
            "status",
            "expected_postcondition",
            "observed_state",
            "confidence",
            "target_bbox",
            "need_refine",
            "ambiguity",
        ],
    }


def _analysis_prompt(
    *,
    expected_postcondition: str,
    target: str,
    purpose: str,
    pass_index: int,
) -> str:
    return (
        "Analyze only the attached current UI image. Do not infer hidden state, "
        "use tools, or claim that an action executed. Return the schema JSON only. "
        "Status meanings: match = the expected visible state is directly shown; "
        "nonmatch = it is directly contradicted; ambiguous = evidence is unclear or "
        "multiple candidates exist; capture_invalid = the frame is blank, corrupt, "
        "occluded, unrelated, or otherwise unsafe; target_not_visible = the requested "
        "target is absent from this valid frame. target_bbox is normalized [0,1] in "
        "this image and must tightly bound one unique target; otherwise null. Set "
        "need_refine only when a tighter crop could resolve a visible candidate.\n"
        f"Purpose: {_bounded_text(purpose, 80)}\n"
        f"Expected visible postcondition: {_bounded_text(expected_postcondition, 500)}\n"
        f"Requested visible target: {_bounded_text(target, 500)}\n"
        f"Adaptive pass: {max(0, int(pass_index))}"
    )


def _extract_json(raw: object) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```").strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        decoder = json.JSONDecoder()
        start = text.find("{")
        if start < 0:
            raise ValueError("visual_verdict_not_json")
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError as exc:
            raise ValueError("visual_verdict_not_json") from exc
    if not isinstance(value, dict):
        raise ValueError("visual_verdict_not_object")
    return value


def parse_visual_verdict(
    raw: object,
    *,
    expected_postcondition: str,
    pass_index: int = 0,
) -> VisualVerdict:
    data = _extract_json(raw)
    required = {
        "status",
        "expected_postcondition",
        "observed_state",
        "confidence",
        "target_bbox",
        "need_refine",
        "ambiguity",
    }
    if set(data) != required:
        raise ValueError("visual_verdict_schema_mismatch")
    try:
        status = VisualStatus(str(data["status"]))
    except ValueError as exc:
        raise ValueError("invalid_visual_status") from exc
    confidence_value = data["confidence"]
    if (
        isinstance(confidence_value, bool)
        or not isinstance(confidence_value, (int, float))
        or not math.isfinite(float(confidence_value))
        or not 0.0 <= float(confidence_value) <= 1.0
    ):
        raise ValueError("invalid_visual_confidence")
    if not isinstance(data["need_refine"], bool):
        raise ValueError("invalid_visual_refine_flag")
    bbox_value = data["target_bbox"]
    bbox = None
    if bbox_value is not None:
        if not isinstance(bbox_value, dict) or set(bbox_value) != {
            "left", "top", "right", "bottom",
        }:
            raise ValueError("invalid_visual_bbox")
        try:
            bbox = NormalizedBBox(**{
                key: float(bbox_value[key])
                for key in ("left", "top", "right", "bottom")
            })
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_visual_bbox") from exc
    if status in {
        VisualStatus.CAPTURE_INVALID,
        VisualStatus.TARGET_NOT_VISIBLE,
        VisualStatus.NONMATCH,
        VisualStatus.STALE,
    } and bbox is not None:
        raise ValueError("bbox_for_nonactionable_visual_status")
    ambiguity = _bounded_text(data["ambiguity"], _MAX_AMBIGUITY_CHARS)
    if status is VisualStatus.AMBIGUOUS and not ambiguity:
        raise ValueError("ambiguous_visual_verdict_without_reason")
    return VisualVerdict(
        status=status,
        expected_postcondition=_bounded_text(
            expected_postcondition or data["expected_postcondition"], 500,
        ),
        observed_state=_bounded_text(
            data["observed_state"], _MAX_OBSERVED_STATE_CHARS,
        ),
        confidence=float(confidence_value),
        target_bbox=bbox,
        need_refine=bool(data["need_refine"]),
        ambiguity=ambiguity,
        pass_index=max(0, int(pass_index)),
        normalized_regions=((bbox,) if bbox is not None else ()),
        refinement_roi=(bbox if bool(data["need_refine"]) else None),
        reason_code="model_visual_evidence",
    )


class ImageInputTransport:
    """Build one image part for the already-active model; never send or reason."""

    def build(
        self,
        frame: VisualFrame,
        prompt: str,
        schema: dict,
        *,
        parent_image_id: str | None = None,
        owner_id: str = "",
    ) -> ImageRequest:
        from osia.image_inputs import (
            content_with_images,
            ensure_image_input_supported,
            image_input_decision,
        )

        if not frame.is_fresh():
            raise VisionTransportError(
                VisualStatus.STALE_FRAME,
                "image_part_frame_stale",
                failure=VisionFailure.TARGET_STALE,
                stage=VisionStage.IMAGE_PART_BUILT,
            )
        decision = image_input_decision(frame.provider, frame.model, (frame.mime,))
        if not decision.usable:
            raise VisionTransportError(
                VisualStatus.UNSUPPORTED,
                "effective_model_image_path_unavailable:" + decision.reason,
                failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
                stage=VisionStage.IMAGE_PART_BUILT,
            )
        if not frame.image_bytes:
            raise VisionTransportError(
                VisualStatus.CAPTURE_INVALID,
                "image_part_empty",
                failure=VisionFailure.IMAGE_PART_EMPTY,
                stage=VisionStage.IMAGE_PART_BUILT,
            )
        from PIL import Image

        try:
            with Image.open(io.BytesIO(frame.image_bytes)) as image:
                actual_dimensions = image.size
        except Exception as exc:
            raise VisionTransportError(
                VisualStatus.CAPTURE_INVALID,
                "image_part_build_failed",
                failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
                stage=VisionStage.IMAGE_PART_BUILT,
            ) from exc
        if actual_dimensions != (frame.image_width, frame.image_height):
            raise VisionTransportError(
                VisualStatus.CAPTURE_INVALID,
                "image_part_dimension_mismatch",
                failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
                stage=VisionStage.IMAGE_PART_BUILT,
            )
        artifact = frame.to_image_artifact(parent_image_id=parent_image_id)
        content = content_with_images(
            prompt,
            (artifact,),
            owner_id=owner_id,
        )
        try:
            ensure_image_input_supported(
                frame.provider,
                frame.model,
                content,
                owner_id=owner_id,
            )
        except Exception as exc:
            raise VisionTransportError(
                VisualStatus.UNSUPPORTED,
                "image_part_build_failed",
                failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
                stage=VisionStage.IMAGE_PART_BUILT,
            ) from exc
        target_binding = hashlib.sha256(
            repr(frame.target_identity or frame.identity_key).encode(
                "utf-8", errors="replace",
            )
        ).hexdigest()
        return ImageRequest(
            provider=frame.provider,
            model=frame.model,
            frame_id=frame.frame_id,
            target_binding=target_binding,
            mime=frame.mime,
            dimensions=(frame.image_width, frame.image_height),
            messages=[{"role": "user", "content": content}],
            schema=schema,
        )

class VisionRuntime:
    """Single canonical perception/action state machine for all domains."""

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.provider = str(provider or "").lower()
        self.model = str(model or "")
        self.clock = clock

    def capability(self):
        from osia.image_inputs import image_input_decision

        return image_input_decision(self.provider, self.model, ("image/png",))

    @property
    def vision_authoritative(self) -> bool:
        return bool(self.capability().usable)

    def resolve_target(
        self,
        semantic_target: str,
        resolver: TargetResolver,
        *,
        trace: VisionTrace | None = None,
    ) -> tuple[VisualTarget, VisionTrace]:
        trace = trace or VisionTrace()
        try:
            target = resolver(str(semantic_target or ""))
        except Exception as exc:
            trace.fail(
                VisionStage.TARGET_RESOLVED,
                VisionFailure.TARGET_RESOLUTION_FAILED,
                "target_resolution_failed",
            )
            raise VisionTargetError("target_resolution_failed") from exc
        if not isinstance(target, VisualTarget):
            trace.fail(
                VisionStage.TARGET_RESOLVED,
                VisionFailure.TARGET_RESOLUTION_FAILED,
                "target_resolver_did_not_return_visual_target",
            )
            raise VisionTargetError(
                "target_resolver_did_not_return_visual_target"
            )
        trace.record(
            VisionStage.TARGET_RESOLVED,
            "semantic_visual_target_resolved",
            **target.safe_dict(),
        )
        return target, trace

    def capture(
        self,
        target: VisualTarget,
        driver: CaptureDriver,
        *,
        trace: VisionTrace | None = None,
        post_action: bool = False,
    ) -> tuple[VisualFrame, VisionTrace]:
        trace = trace or VisionTrace()
        if not target.visible or target.minimized:
            trace.fail(
                VisionStage.CAPTURE_REQUESTED,
                VisionFailure.TARGET_STALE,
                "minimized_target",
            )
            raise VisionCaptureError(CaptureFailure.MINIMIZED_TARGET)
        trace.record(
            VisionStage.CAPTURE_REQUESTED,
            "post_action_exact_capture_requested"
            if post_action else "exact_bound_capture_requested",
            domain=target.domain.value,
            surface_id=target.surface_id,
            page_id=target.page_id,
        )
        try:
            frame = driver(target)
        except VisionCaptureError as exc:
            trace.fail(
                VisionStage.CAPTURE_REQUESTED,
                _capture_failure_class(exc.failure),
                exc.reason_code,
            )
            raise
        except PermissionError as exc:
            trace.fail(
                VisionStage.CAPTURE_REQUESTED,
                VisionFailure.CAPTURE_ACCESS_DENIED,
                "capture_access_denied",
            )
            raise VisionCaptureError(
                CaptureFailure.ACCESS_DENIED,
                "capture_access_denied",
            ) from exc
        except Exception as exc:
            trace.fail(
                VisionStage.CAPTURE_REQUESTED,
                VisionFailure.CAPTURE_DEVICE_FAILED,
                "capture_device_failed",
            )
            raise VisionCaptureError(
                CaptureFailure.DEVICE_LOSS,
                "capture_device_failed",
            ) from exc
        if not isinstance(frame, VisualFrame):
            trace.fail(
                VisionStage.CAPTURE_COMPLETED,
                VisionFailure.CAPTURE_DEVICE_FAILED,
                "capture_driver_did_not_return_visual_frame",
            )
            raise VisionCaptureError(
                CaptureFailure.DEVICE_LOSS,
                "capture_driver_did_not_return_visual_frame",
            )
        if frame.provider != self.provider or frame.model != self.model:
            trace.fail(
                VisionStage.CAPTURE_COMPLETED,
                VisionFailure.TARGET_STALE,
                "capture_provider_model_binding_mismatch",
            )
            raise VisionCaptureError(
                CaptureFailure.STALE_TARGET,
                "capture_provider_model_binding_mismatch",
            )
        if frame.domain is not target.domain:
            trace.fail(
                VisionStage.CAPTURE_COMPLETED,
                VisionFailure.TARGET_STALE,
                "capture_domain_mismatch",
            )
            raise VisionCaptureError(CaptureFailure.STALE_TARGET, "capture_domain_mismatch")
        if frame.target_identity != target.identity_key:
            trace.fail(
                VisionStage.CAPTURE_COMPLETED,
                VisionFailure.TARGET_STALE,
                "generation_mismatch",
            )
            raise VisionCaptureError(CaptureFailure.GENERATION_MISMATCH)
        if frame.image_width <= 0 or frame.image_height <= 0:
            trace.fail(
                VisionStage.CAPTURE_COMPLETED,
                VisionFailure.CAPTURE_EMPTY,
                "empty_frame",
            )
            raise VisionCaptureError(CaptureFailure.EMPTY_FRAME)
        trace.record(
            VisionStage.POST_CAPTURE_COMPLETED
            if post_action else VisionStage.CAPTURE_COMPLETED,
            "post_action_exact_capture_completed"
            if post_action else "exact_bound_capture_completed",
            frame_id=frame.frame_id,
            dimensions=[frame.image_width, frame.image_height],
            capture_driver=frame.capture_method,
            image_sha256=frame.image_sha256,
            exact_surface_pixels=frame.exact_surface_pixels,
        )
        return frame, trace

    def refine(
        self,
        frame: VisualFrame,
        roi: NormalizedBBox,
        *,
        trace: VisionTrace | None = None,
        pass_index: int = 1,
    ) -> VisualFrame:
        """Produce one bounded crop while retaining the canonical transform."""

        if trace is not None:
            trace.record(
                VisionStage.REFINE_REQUESTED,
                "bounded_roi_refinement_requested",
                source_frame_id=frame.frame_id,
                pass_index=max(1, int(pass_index)),
                roi=roi.to_dict(),
            )
        return crop_capture(frame, roi)

    def propose_action(
        self,
        analysis: VisualAnalysis,
        *,
        semantic_target: str,
        action_type: str,
        expected_postcondition: str,
        structured_actuator: str = "",
        trace: VisionTrace | None = None,
    ) -> VisualActionProposal:
        verdict = analysis.verdict
        bbox = verdict.target_bbox
        if bbox is None:
            raise ValueError("visual_action_requires_unique_bbox")
        inset_x = min(bbox.width * 0.2, 0.02)
        inset_y = min(bbox.height * 0.2, 0.02)
        safe_bbox = NormalizedBBox(
            bbox.left + inset_x,
            bbox.top + inset_y,
            bbox.right - inset_x,
            bbox.bottom - inset_y,
        )
        proposal = VisualActionProposal(
            proposal_id="vprop_" + secrets.token_urlsafe(15),
            source_frame_id=analysis.capture.frame_id,
            target_identity=(
                analysis.capture.target_identity
                or analysis.capture.identity_key
            ),
            semantic_target=_bounded_text(semantic_target, 500),
            action_type=_bounded_text(action_type, 80),
            normalized_bbox=bbox,
            confidence=verdict.confidence,
            ambiguous=bool(
                verdict.status is not VisualStatus.MATCH
                or verdict.ambiguity
                or verdict.need_refine
            ),
            expected_postcondition=_bounded_text(expected_postcondition, 500),
            required_foreground=True,
            safe_interior_bbox=safe_bbox,
            expires_at=min(
                analysis.capture.expires_at,
                analysis.capture.captured_at + 5.0,
            ),
            source_geometry=_frame_geometry_identity(analysis.capture),
            source_transform=analysis.capture.transform,
            structured_actuator=_bounded_text(structured_actuator, 120),
        )
        if trace is not None:
            trace.record(
                VisionStage.ACTION_PROPOSED,
                "visual_action_proposal_created",
                **proposal.safe_dict(),
            )
        return proposal

    @staticmethod
    def select_actuator(
        proposal: VisualActionProposal,
        *,
        structured_target_valid: bool,
    ) -> str:
        """Prefer a freshly correlated structured actuator, else local mouse."""

        if proposal.structured_actuator and structured_target_valid:
            return proposal.structured_actuator
        return "vision_local_mouse"

    def preflight(
        self,
        proposal: VisualActionProposal,
        fresh_frame: VisualFrame,
        *,
        permission_valid: bool = True,
        effect_class_valid: bool = True,
        occluded: bool = False,
        trace: VisionTrace | None = None,
    ) -> VisualPreflight:
        reason = "visual_action_preflight_passed"
        reobserve = False
        passed = True
        if not permission_valid or not effect_class_valid:
            passed, reason = False, "visual_action_permission_invalid"
        elif proposal.ambiguous or proposal.confidence < _MIN_CLICK_CONFIDENCE:
            passed, reason = False, "visual_action_ambiguous"
        elif self.clock() > proposal.expires_at or not fresh_frame.is_fresh(
            now=self.clock()
        ):
            passed, reason, reobserve = False, "visual_action_frame_expired", True
        elif fresh_frame.frame_id == proposal.source_frame_id:
            # Preflight must use a distinct fresh capture, never the proposal frame.
            passed, reason, reobserve = False, "visual_action_fresh_capture_required", True
        elif (
            fresh_frame.target_identity or fresh_frame.identity_key
        ) != proposal.target_identity:
            passed, reason, reobserve = False, "visual_action_target_stale", True
        elif _frame_geometry_identity(fresh_frame) != proposal.source_geometry:
            passed, reason, reobserve = (
                False, "visual_action_geometry_changed", True
            )
        elif proposal.required_foreground and not fresh_frame.foreground:
            passed, reason, reobserve = False, "visual_action_target_not_foreground", True
        elif occluded:
            passed, reason, reobserve = False, "visual_action_target_occluded", True
        point = None
        if passed:
            transform = proposal.source_transform
            if transform is None:
                passed, reason = False, "visual_action_transform_missing"
            else:
                left, top, right, bottom = transform.bbox_screen_pixels(
                    proposal.safe_interior_bbox
                )
                point = ((left + right) // 2, (top + bottom) // 2)
                bounds = transform.screen_bounds
                if not (
                    bounds[0] <= point[0] < bounds[2]
                    and bounds[1] <= point[1] < bounds[3]
                ):
                    passed, reason, point = False, "visual_action_point_outside_target", None
        result = VisualPreflight(passed, reason, point, reobserve)
        if passed and trace is not None:
            trace.record(
                VisionStage.PREFLIGHT_PASSED,
                reason,
                proposal_id=proposal.proposal_id,
                point=list(point or ()),
                fresh_frame_id=fresh_frame.frame_id,
            )
        elif trace is not None:
            trace.fail(
                VisionStage.PREFLIGHT_PASSED,
                (
                    VisionFailure.VERDICT_AMBIGUOUS
                    if reason == "visual_action_ambiguous" else
                    VisionFailure.TARGET_STALE
                ),
                reason,
            )
        return result

    def reconcile(
        self,
        *,
        pre_frame: VisualFrame,
        post_frame: VisualFrame | None,
        verdict: VisualVerdict | None,
        observer_error: bool = False,
        no_partial_effect: bool = False,
        trace: VisionTrace | None = None,
    ) -> VisualReconciliation:
        if observer_error or post_frame is None or verdict is None:
            status = VisualReconciliationStatus.OBSERVER_FAILED
            reason = "post_action_visual_observer_failed"
        elif post_frame.window_identity_key != pre_frame.window_identity_key:
            status = VisualReconciliationStatus.TARGET_STALE
            reason = "post_action_visual_target_stale"
        elif verdict.status is VisualStatus.MATCH:
            status = VisualReconciliationStatus.EFFECT_VERIFIED
            reason = "post_action_visual_effect_verified"
        elif verdict.status is VisualStatus.NONMATCH and no_partial_effect:
            status = VisualReconciliationStatus.NOT_APPLIED_VERIFIED
            reason = "post_action_visual_non_application_verified"
        else:
            status = VisualReconciliationStatus.EFFECT_POSSIBLE_RECONCILING
            reason = "post_action_visual_effect_possible"
        result = VisualReconciliation(
            status,
            reason,
            pre_frame.frame_id,
            post_frame.frame_id if post_frame is not None else "",
            verdict,
        )
        if trace is not None:
            reconciliation_metadata = result.safe_dict()
            reconciliation_metadata["reconciliation_reason_code"] = (
                reconciliation_metadata.pop("reason_code", "")
            )
            trace.record(
                VisionStage.RECONCILED,
                reason,
                **reconciliation_metadata,
            )
        return result


class WindowsCaptureDriver:
    """Strict adapter for an exact-HWND frame producer.

    Windows Graphics Capture/DWM remains the primary implementation supplied by
    ``desktop_scene``. This boundary rejects foreground/global substitutions and
    converts backend conditions into typed capture failures.
    """

    def __init__(self, capture_exact: Callable[[VisualTarget], VisualFrame]):
        self._capture_exact = capture_exact

    def __call__(self, target: VisualTarget) -> VisualFrame:
        if sys.platform != "win32":
            raise VisionCaptureError(CaptureFailure.UNSUPPORTED)
        if target.domain is not VisionDomain.DESKTOP or not target.native_window_id:
            raise VisionCaptureError(CaptureFailure.STALE_TARGET)
        if target.minimized:
            raise VisionCaptureError(CaptureFailure.MINIMIZED_TARGET)
        try:
            frame = self._capture_exact(target)
        except VisionCaptureError:
            raise
        except PermissionError as exc:
            raise VisionCaptureError(CaptureFailure.ACCESS_DENIED) from exc
        except Exception as exc:
            reason = str(exc or "").lower()
            failure = next((
                item for token, item in (
                    ("protected", CaptureFailure.PROTECTED_CONTENT),
                    ("minimized", CaptureFailure.MINIMIZED_TARGET),
                    ("generation", CaptureFailure.GENERATION_MISMATCH),
                    ("resize", CaptureFailure.RESIZED_DURING_CAPTURE),
                    ("black", CaptureFailure.BLACK_FRAME),
                    ("empty", CaptureFailure.EMPTY_FRAME),
                    ("device", CaptureFailure.DEVICE_LOSS),
                    ("dimension", CaptureFailure.WRONG_DIMENSIONS),
                    ("stale", CaptureFailure.STALE_TARGET),
                ) if token in reason
            ), CaptureFailure.UNSUPPORTED)
            raise VisionCaptureError(failure) from exc
        if not frame.exact_surface_pixels:
            raise VisionCaptureError(
                CaptureFailure.UNSUPPORTED,
                "windows_exact_surface_capture_required",
            )
        transform = frame.transform
        window_bounds = frame.window_bounds
        if transform is None or window_bounds is None:
            raise VisionCaptureError(CaptureFailure.WRONG_DIMENSIONS)
        screen_bounds = transform.screen_bounds
        expected_width = int(screen_bounds[2]) - int(screen_bounds[0])
        expected_height = int(screen_bounds[3]) - int(screen_bounds[1])
        window_left, window_top, window_right, window_bottom = (
            int(value) for value in window_bounds
        )
        scale_x = expected_width / float(transform.source_width)
        scale_y = expected_height / float(transform.source_height)
        anisotropy = abs(scale_x - scale_y) / max(scale_x, scale_y)
        if (
            expected_width <= 0
            or expected_height <= 0
            or int(screen_bounds[0]) < window_left - 8
            or int(screen_bounds[1]) < window_top - 8
            or int(screen_bounds[2]) > window_right + 8
            or int(screen_bounds[3]) > window_bottom + 8
            or not (0.25 <= scale_x <= 4.0 and 0.25 <= scale_y <= 4.0)
            or anisotropy > 0.05
        ):
            raise VisionCaptureError(
                CaptureFailure.WRONG_DIMENSIONS,
                "capture_transform_geometry_mismatch",
            )
        return frame


class BrowserCaptureDriver:
    """Exact page/locator screenshot adapter bound to WOR-269 page identity."""

    def __init__(
        self,
        provider: str,
        model: str,
        screenshot: Callable[[VisualTarget], bytes],
        *,
        capture_method: str = "browser_exact_page",
    ):
        self.provider = str(provider or "").lower()
        self.model = str(model or "")
        self._screenshot = screenshot
        self.capture_method = _bounded_text(capture_method, 80)

    def __call__(self, target: VisualTarget) -> VisualFrame:
        if target.domain is not VisionDomain.BROWSER:
            raise VisionCaptureError(CaptureFailure.STALE_TARGET)
        try:
            png = self._screenshot(target)
        except Exception as exc:
            raise VisionCaptureError(CaptureFailure.UNSUPPORTED) from exc
        from PIL import Image

        with Image.open(io.BytesIO(png)) as opened:
            width, height = opened.size
        transform = CoordinateTransform(
            source_width=width,
            source_height=height,
            source_box=(0.0, 0.0, float(width), float(height)),
            screen_bounds=(0, 0, width, height),
            client_origin_screen=(0, 0),
            dpi_scale=target.dpi_scale,
            monitor_id=target.monitor_id,
            window_bounds=target.bounds,
            client_bounds=(0, 0, width, height),
        )
        return prepare_capture(
            png,
            provider=self.provider,
            model=self.model,
            application_id=target.application_id,
            surface_ref=target.surface_id,
            window_generation=target.surface_generation,
            structural_epoch=target.document_epoch,
            transform=transform,
            native_window_token="",
            capture_method=self.capture_method,
            exact_surface_pixels=True,
            foreground=True,
            domain=VisionDomain.BROWSER,
            target_identity=target.identity_key,
            page_id=target.page_id,
            page_generation=target.page_generation,
            browser_frame_id=target.frame_id,
            document_epoch=target.document_epoch,
            monitor_id=target.monitor_id,
            window_bounds=target.bounds,
            client_bounds=(0, 0, width, height),
        )


class GlobalCaptureDriver:
    """Explicit global-context exception using the canonical frame contract."""

    def __init__(
        self,
        provider: str,
        model: str,
        screenshot: Callable[[VisualTarget], bytes],
    ):
        self.provider = str(provider or "").lower()
        self.model = str(model or "")
        self._screenshot = screenshot

    def __call__(self, target: VisualTarget) -> VisualFrame:
        if (
            target.domain is not VisionDomain.GLOBAL
            or not target.allow_global_context
        ):
            raise VisionCaptureError(
                CaptureFailure.UNSUPPORTED,
                "global_capture_requires_explicit_exception",
            )
        png = self._screenshot(target)
        from PIL import Image

        with Image.open(io.BytesIO(png)) as opened:
            width, height = opened.size
        transform = CoordinateTransform(
            source_width=width,
            source_height=height,
            source_box=(0.0, 0.0, float(width), float(height)),
            screen_bounds=target.bounds,
            client_origin_screen=(target.bounds[0], target.bounds[1]),
            dpi_scale=target.dpi_scale,
            monitor_id=target.monitor_id,
            window_bounds=target.bounds,
            client_bounds=(0, 0, width, height),
        )
        return prepare_capture(
            png,
            provider=self.provider,
            model=self.model,
            application_id=target.application_id,
            surface_ref=target.surface_id,
            window_generation=target.surface_generation,
            structural_epoch=0,
            transform=transform,
            capture_method="explicit_global_context",
            exact_surface_pixels=False,
            domain=VisionDomain.GLOBAL,
            target_identity=target.identity_key,
            monitor_id=target.monitor_id,
            window_bounds=target.bounds,
            client_bounds=(0, 0, width, height),
            screen_origin=(target.bounds[0], target.bounds[1]),
        )

def click_point(analysis: VisualAnalysis) -> tuple[int, int] | None:
    """Return a safe physical-screen point, never a guessed coordinate."""

    if not analysis.verdict.click_eligible or analysis.capture.transform is None:
        return None
    left, top, right, bottom = analysis.capture.transform.bbox_screen_pixels(
        analysis.verdict.target_bbox,
    )
    if right <= left or bottom <= top:
        return None
    point = ((left + right) // 2, (top + bottom) // 2)
    screen_left, screen_top, screen_right, screen_bottom = (
        analysis.capture.transform.screen_bounds
    )
    if not (
        screen_left <= point[0] < screen_right
        and screen_top <= point[1] < screen_bottom
    ):
        return None
    return point


__all__ = [
    "BrowserCaptureDriver",
    "BoundVisualCapture",
    "CaptureFailure",
    "CoordinateTransform",
    "FrameFreshness",
    "GlobalCaptureDriver",
    "ImageInputTransport",
    "ImageRequest",
    "NormalizedBBox",
    "StageReceipt",
    "VisionCaptureError",
    "VisionDomain",
    "VisionFailure",
    "VisionModelOutputError",
    "VisionRuntime",
    "VisionStage",
    "VisionTargetError",
    "VisionTrace",
    "VisionTransportError",
    "VisualActionProposal",
    "VisualAnalysis",
    "VisualFrame",
    "VisualPreflight",
    "VisualReconciliation",
    "VisualReconciliationStatus",
    "VisualStatus",
    "VisualTarget",
    "VisualVerdict",
    "WindowsCaptureDriver",
    "click_point",
    "crop_capture",
    "parse_visual_verdict",
    "prepare_capture",
    "visual_frame_pixels_unchanged",
]
