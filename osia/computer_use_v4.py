"""Small same-model computer-use loop for exact native-window frames.

This module deliberately owns only four things: a frame, an explicit image to
screen transform, a local stale check, and one ActionTruth dispatch boundary.
It does not depend on the older Vision proposal/lease/verdict orchestration.
"""

from __future__ import annotations

import hashlib
import io
import math
import secrets
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping


_MUTATING_ACTIONS = frozenset({
    "click", "double_click", "type", "keypress", "scroll",
})
_MAX_BATCH_ACTIONS = 3
_DEFAULT_FRAME_TTL_SECONDS = 120.0
_DEFAULT_MAX_IMAGE_DIMENSION = 2048


class ComputerUseV4Error(RuntimeError):
    """One typed, fail-closed V4 error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "COMPUTER_USE_FAILED")[:80]
        self.detail = str(detail or "")[:160]
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class ComputerFrame:
    """Minimal immutable image and window authority for one V4 observation."""

    frame_id: str
    loop_id: str
    turn_id: str
    provider: str
    model: str
    hwnd: int
    pid: int
    process_started_at: float
    window_class: str
    window_title: str
    window_token: str
    captured_at: float
    expires_at: float
    original_width: int
    original_height: int
    image_width: int
    image_height: int
    source_box: tuple[float, float, float, float]
    capture_screen_bounds: tuple[int, int, int, int]
    window_bounds: tuple[int, int, int, int]
    client_bounds: tuple[int, int, int, int]
    client_origin_screen: tuple[int, int]
    dpi_scale: float
    image_bytes: bytes = field(repr=False)
    scene_sha256: str
    image_sha256: str
    capture_method: str
    capture_attempts: tuple[dict, ...] = ()
    parent_frame_id: str = ""
    foreground_at_capture: bool | None = None

    def __post_init__(self) -> None:
        if not self.frame_id or not self.loop_id or not self.turn_id:
            raise ValueError("invalid_frame_identity")
        if self.hwnd <= 0 or self.pid <= 0 or not self.window_token:
            raise ValueError("invalid_window_identity")
        if self.original_width <= 0 or self.original_height <= 0:
            raise ValueError("invalid_original_dimensions")
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("invalid_image_dimensions")
        if not self.image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid_frame_png")
        if hashlib.sha256(self.image_bytes).hexdigest() != self.image_sha256:
            raise ValueError("frame_image_hash_mismatch")
        if not math.isfinite(self.captured_at) or self.captured_at <= 0:
            raise ValueError("invalid_capture_timestamp")
        if self.expires_at <= self.captured_at:
            raise ValueError("invalid_frame_expiry")
        left, top, right, bottom = self.source_box
        if not (
            0.0 <= left < right <= float(self.original_width)
            and 0.0 <= top < bottom <= float(self.original_height)
        ):
            raise ValueError("invalid_frame_source_box")
        screen_left, screen_top, screen_right, screen_bottom = (
            self.capture_screen_bounds
        )
        if screen_right <= screen_left or screen_bottom <= screen_top:
            raise ValueError("invalid_capture_screen_bounds")
        if not math.isfinite(self.dpi_scale) or self.dpi_scale <= 0:
            raise ValueError("invalid_frame_dpi_scale")

    def map_image_point_to_screen(self, x: float, y: float) -> tuple[int, int]:
        """Map exactly what the model saw to physical screen coordinates."""

        try:
            image_x = float(x)
            image_y = float(y)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ComputerUseV4Error("OUT_OF_BOUNDS", "non_numeric_point") from exc
        if not (
            math.isfinite(image_x)
            and math.isfinite(image_y)
            and 0.0 <= image_x < float(self.image_width)
            and 0.0 <= image_y < float(self.image_height)
        ):
            raise ComputerUseV4Error("OUT_OF_BOUNDS", "image_point_outside_frame")

        crop_left, crop_top, crop_right, crop_bottom = self.source_box
        source_x = crop_left + (
            image_x / float(self.image_width)
        ) * (crop_right - crop_left)
        source_y = crop_top + (
            image_y / float(self.image_height)
        ) * (crop_bottom - crop_top)
        screen_left, screen_top, screen_right, screen_bottom = (
            self.capture_screen_bounds
        )
        screen_x = screen_left + (
            source_x / float(self.original_width)
        ) * (screen_right - screen_left)
        screen_y = screen_top + (
            source_y / float(self.original_height)
        ) * (screen_bottom - screen_top)
        mapped = int(round(screen_x)), int(round(screen_y))
        if not (
            screen_left <= mapped[0] < screen_right
            and screen_top <= mapped[1] < screen_bottom
        ):
            raise ComputerUseV4Error("OUT_OF_BOUNDS", "mapped_point_outside_capture")
        return mapped

    def safe_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "loop_id": self.loop_id,
            "turn_id": self.turn_id,
            "provider": self.provider,
            "model": self.model,
            "hwnd": str(self.hwnd),
            "captured_at": round(self.captured_at, 6),
            "expires_at": round(self.expires_at, 6),
            "original_dimensions": [self.original_width, self.original_height],
            "image_dimensions": [self.image_width, self.image_height],
            "source_box": [round(value, 3) for value in self.source_box],
            "capture_screen_bounds": list(self.capture_screen_bounds),
            "window_bounds": list(self.window_bounds),
            "client_bounds": list(self.client_bounds),
            "client_origin_screen": list(self.client_origin_screen),
            "dpi_scale": round(self.dpi_scale, 4),
            "image_sha256": self.image_sha256,
            "capture_method": self.capture_method,
            "foreground_at_capture": self.foreground_at_capture,
            "capture_attempts": [dict(item) for item in self.capture_attempts],
            "parent_frame_id": self.parent_frame_id,
            "mapping_chain": (
                "model_image_pixels_to_original_capture_pixels_to_"
                "window_client_coordinates_to_physical_screen"
            ),
        }

    def to_image_artifact(self):
        """Use OSIA's canonical image artifact without a Vision V3 frame."""

        from osia.image_inputs import ImageSourceType, create_image_artifact

        return create_image_artifact(
            self.image_bytes,
            "image/png",
            (
                ImageSourceType.REFINED_CROP
                if self.parent_frame_id else ImageSourceType.DESKTOP_CAPTURE
            ),
            created_at=self.captured_at,
            hwnd=str(self.hwnd),
            surface_id="v4_" + self.window_token,
            generation=self.window_token,
            application_identity="v4_" + self.window_token,
            bounds=self.window_bounds,
            dpi=max(1, int(round(self.dpi_scale * 96.0))),
            freshness="fresh",
            source_dimensions=(self.original_width, self.original_height),
            provider_dimensions=(self.image_width, self.image_height),
            detail_level="high",
            crop_transform={
                "source_dimensions": [self.original_width, self.original_height],
                "source_box": list(self.source_box),
                "capture_screen_bounds": list(self.capture_screen_bounds),
            },
            expires_at=self.expires_at,
            capture_method=self.capture_method,
            capture_attempts=self.capture_attempts,
            fallback_used=len(self.capture_attempts) > 1,
        )


@dataclass(frozen=True, repr=False)
class ComputerAction:
    """One model-requested V4 operation bound to the frame it observed."""

    kind: str
    frame_id: str
    x: float | None = None
    y: float | None = None
    text: str = field(default="", repr=False)
    key: str = ""
    direction: str = "down"
    amount: int = 3
    observe_after: bool = False
    effect_id: str = ""
    target_hwnd: int | None = None

    def __post_init__(self) -> None:
        normalized = str(self.kind or "").strip().lower()
        object.__setattr__(self, "kind", normalized)
        if normalized not in _MUTATING_ACTIONS:
            raise ValueError("unsupported_computer_action")
        if not self.frame_id:
            raise ValueError("computer_action_requires_frame")
        if normalized in {"click", "double_click"} and (
            self.x is None or self.y is None
        ):
            raise ValueError("computer_click_requires_coordinates")
        if normalized == "type" and self.text is None:
            raise ValueError("computer_type_requires_text")
        if normalized == "keypress" and not str(self.key or "").strip():
            raise ValueError("computer_keypress_requires_key")
        if normalized == "scroll":
            if str(self.direction or "").lower() not in {
                "up", "down", "left", "right",
            }:
                raise ValueError("invalid_scroll_direction")
            object.__setattr__(self, "amount", max(1, min(int(self.amount), 50)))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ComputerAction":
        if not isinstance(value, Mapping):
            raise ValueError("computer_action_not_mapping")
        target_hwnd = value.get("target_hwnd")
        return cls(
            kind=str(value.get("kind") or ""),
            frame_id=str(value.get("frame_id") or ""),
            x=value.get("x"),
            y=value.get("y"),
            text=str(value.get("text") or ""),
            key=str(value.get("key") or ""),
            direction=str(value.get("direction") or "down"),
            amount=int(value.get("amount") or 3),
            observe_after=value.get("observe_after") is True,
            effect_id=str(value.get("effect_id") or ""),
            target_hwnd=(
                int(target_hwnd) if target_hwnd not in {None, ""} else None
            ),
        )

    def safe_arguments(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "frame_id": self.frame_id,
            "observe_after": self.observe_after,
        }
        if self.kind in {"click", "double_click"}:
            payload.update({"x": self.x, "y": self.y})
        elif self.kind == "type":
            payload["text_length"] = len(self.text)
            payload["text_sha256"] = hashlib.sha256(
                self.text.encode("utf-8", errors="replace")
            ).hexdigest()
        elif self.kind == "keypress":
            payload["key"] = self.key
        elif self.kind == "scroll":
            payload.update({"direction": self.direction, "amount": self.amount})
        return payload


def computer_action_schema() -> dict[str, Any]:
    """Structured same-model decision contract used by the isolated probe."""

    nullable_number = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["action", "complete"]},
            "message": {"type": "string"},
            "actions": {
                "type": "array",
                "maxItems": _MAX_BATCH_ACTIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "click", "double_click", "type", "keypress",
                                "scroll", "zoom",
                            ],
                        },
                        "frame_id": {"type": "string"},
                        "x": nullable_number,
                        "y": nullable_number,
                        "text": {"type": "string"},
                        "key": {"type": "string"},
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                        },
                        "amount": {
                            "type": "integer", "minimum": 1, "maximum": 50,
                        },
                        "left": nullable_number,
                        "top": nullable_number,
                        "right": nullable_number,
                        "bottom": nullable_number,
                        "observe_after": {"type": "boolean"},
                        "effect_id": {"type": "string"},
                    },
                    "required": [
                        "kind", "frame_id", "x", "y", "text", "key",
                        "direction", "amount", "left", "top", "right",
                        "bottom", "observe_after", "effect_id",
                    ],
                },
            },
        },
        "required": ["status", "message", "actions"],
    }


def _frame_png(image, maximum_dimension: int) -> tuple[bytes, int, int]:
    from PIL import Image

    converted = image.convert("RGB")
    limit = max(256, min(int(maximum_dimension), 4096))
    if max(converted.size) > limit:
        scale = limit / float(max(converted.size))
        converted = converted.resize(
            (
                max(1, int(round(converted.width * scale))),
                max(1, int(round(converted.height * scale))),
            ),
            getattr(Image, "Resampling", Image).LANCZOS,
        )
    output = io.BytesIO()
    converted.save(output, format="PNG", optimize=True)
    return output.getvalue(), converted.width, converted.height


def _scene_fingerprint(image) -> str:
    """Return diagnostic frame identity; never use it as stale authority."""

    material = (
        f"{image.width}x{image.height}:RGB:".encode("ascii")
        + image.convert("RGB").tobytes()
    )
    return hashlib.sha256(material).hexdigest()


def _client_scene_fingerprint(
    image,
    state: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> str:
    """Hash client pixels for receipts/debugging, never action validity."""

    capture = provenance.get("capture_screen_bounds")
    origin = state.get("client_origin_screen")
    client = state.get("client_bounds")
    if not (
        isinstance(capture, (list, tuple)) and len(capture) == 4
        and isinstance(origin, (list, tuple)) and len(origin) == 2
        and isinstance(client, (list, tuple)) and len(client) == 4
    ):
        raise ComputerUseV4Error(
            "CAPTURE_FAILED", "client_fingerprint_geometry_unavailable",
        )
    capture_left, capture_top, capture_right, capture_bottom = (
        int(value) for value in capture
    )
    client_left = int(origin[0]) + int(client[0])
    client_top = int(origin[1]) + int(client[1])
    client_right = int(origin[0]) + int(client[2])
    client_bottom = int(origin[1]) + int(client[3])
    bounded = (
        max(capture_left, client_left),
        max(capture_top, client_top),
        min(capture_right, client_right),
        min(capture_bottom, client_bottom),
    )
    if bounded[2] <= bounded[0] or bounded[3] <= bounded[1]:
        raise ComputerUseV4Error("CAPTURE_FAILED", "client_fingerprint_empty")
    scale_x = image.width / float(capture_right - capture_left)
    scale_y = image.height / float(capture_bottom - capture_top)
    pixel_box = (
        max(0, int(math.floor((bounded[0] - capture_left) * scale_x))),
        max(0, int(math.floor((bounded[1] - capture_top) * scale_y))),
        min(image.width, int(math.ceil((bounded[2] - capture_left) * scale_x))),
        min(image.height, int(math.ceil((bounded[3] - capture_top) * scale_y))),
    )
    if pixel_box[2] <= pixel_box[0] or pixel_box[3] <= pixel_box[1]:
        raise ComputerUseV4Error("CAPTURE_FAILED", "client_fingerprint_empty")
    return _scene_fingerprint(image.crop(pixel_box))


def _structural_edge_map(image):
    """Build a small luminance-invariant local structure map."""

    import numpy as np
    from PIL import ImageFilter, ImageOps

    grayscale = ImageOps.grayscale(image).filter(
        ImageFilter.GaussianBlur(radius=1.0),
    )
    values = np.asarray(grayscale, dtype=np.float32)
    horizontal = np.zeros_like(values)
    vertical = np.zeros_like(values)
    if values.shape[1] >= 3:
        horizontal[:, 1:-1] = np.abs(
            values[:, 2:] - values[:, :-2]
        ) * 0.5
    if values.shape[0] >= 3:
        vertical[1:-1, :] = np.abs(
            values[2:, :] - values[:-2, :]
        ) * 0.5
    magnitude = np.maximum(horizontal, vertical)
    material = magnitude[magnitude >= 3.0]
    if material.size < 12:
        return np.zeros_like(magnitude, dtype=bool)
    threshold = max(4.0, float(np.percentile(material, 55.0)))
    return magnitude >= threshold


def _dilate_structure(edges, radius: int = 2):
    import numpy as np

    expanded = np.zeros_like(edges, dtype=bool)
    height, width = edges.shape
    for offset_y in range(-radius, radius + 1):
        source_top = max(0, -offset_y)
        source_bottom = min(height, height - offset_y)
        target_top = source_top + offset_y
        target_bottom = source_bottom + offset_y
        for offset_x in range(-radius, radius + 1):
            source_left = max(0, -offset_x)
            source_right = min(width, width - offset_x)
            target_left = source_left + offset_x
            target_right = source_right + offset_x
            expanded[
                target_top:target_bottom, target_left:target_right
            ] |= edges[
                source_top:source_bottom, source_left:source_right
            ]
    return expanded


def _target_structure_validity(
    frame: ComputerFrame,
    current_image,
    *,
    x: float,
    y: float,
) -> tuple[bool, str]:
    """Check whether the clicked local structure remains at the same point.

    This deliberately ignores RGB equality. It compares blurred luminance
    edges near the chosen point, tolerates two renderer pixels of drift, and
    uses a wider search only to classify a material translation as stale.
    """

    import numpy as np
    from PIL import Image

    if current_image.size != (
        frame.original_width, frame.original_height,
    ):
        return False, "capture_geometry_changed"
    try:
        with Image.open(io.BytesIO(frame.image_bytes)) as opened:
            opened.load()
            source = opened.convert("RGB")
        current = current_image.convert("RGB").crop(frame.source_box)
        current = current.resize(
            (frame.image_width, frame.image_height),
            getattr(Image, "Resampling", Image).LANCZOS,
        )
    except Exception:
        return False, "target_structure_unavailable"

    center_x = int(round(float(x)))
    center_y = int(round(float(y)))
    radius = min(
        72,
        max(48, int(round(min(frame.image_width, frame.image_height) * 0.10))),
    )
    search_radius = min(40, radius)
    left = max(0, center_x - radius - search_radius)
    top = max(0, center_y - radius - search_radius)
    right = min(frame.image_width, center_x + radius + search_radius + 1)
    bottom = min(frame.image_height, center_y + radius + search_radius + 1)
    if right - left < 8 or bottom - top < 8:
        return False, "target_structure_unavailable"

    source_edges = _structural_edge_map(source.crop((left, top, right, bottom)))
    current_edges = _structural_edge_map(current.crop((left, top, right, bottom)))
    rows, columns = np.indices(source_edges.shape)
    local_x = center_x - left
    local_y = center_y - top
    distances = np.hypot(columns - local_x, rows - local_y)
    target_edges = source_edges & (distances <= float(radius))
    edge_y, edge_x = np.nonzero(target_edges)
    if edge_x.size < 12:
        return False, "target_structure_unavailable"
    weights = 1.0 + 2.0 * np.exp(
        -((distances[edge_y, edge_x] / max(1.0, radius * 0.60)) ** 2)
    )
    weight_total = float(weights.sum())
    current_near = _dilate_structure(current_edges, radius=2)

    def alignment(offset_x: int, offset_y: int) -> float:
        shifted_x = edge_x + int(offset_x)
        shifted_y = edge_y + int(offset_y)
        inside = (
            (shifted_x >= 0)
            & (shifted_x < current_near.shape[1])
            & (shifted_y >= 0)
            & (shifted_y < current_near.shape[0])
        )
        if not bool(inside.any()):
            return 0.0
        matched = np.zeros_like(inside, dtype=bool)
        matched[inside] = current_near[
            shifted_y[inside], shifted_x[inside]
        ]
        return float(weights[matched].sum()) / weight_total

    same_position = alignment(0, 0)
    if same_position >= 0.58:
        return True, "target_structure_aligned"

    best_score = same_position
    best_offset = (0, 0)
    for offset_y in range(-search_radius, search_radius + 1, 2):
        for offset_x in range(-search_radius, search_radius + 1, 2):
            if offset_x == 0 and offset_y == 0:
                continue
            score = alignment(offset_x, offset_y)
            if score > best_score:
                best_score = score
                best_offset = (offset_x, offset_y)
    if best_score >= 0.58 and max(abs(value) for value in best_offset) > 3:
        return False, "target_structure_moved"
    return False, "target_structure_changed"


def _default_window_state(hwnd: int) -> dict[str, Any] | None:
    if sys.platform != "win32":
        return None
    try:
        from osia import desktop_scene
        from osia.tools import desktop_control

        title, pid, class_name, bounds = desktop_scene._window_metadata(hwnd)
        if not pid:
            return None
        minimized = desktop_scene._window_is_minimized(hwnd)
        if bounds is None or not bounds.valid():
            return {"hwnd": int(hwnd), "minimized": True} if minimized else None
        _application, process_started_at = desktop_scene._process_details(pid)
        client = desktop_scene._window_client_bounds(hwnd)
        client_origin = desktop_scene._window_client_origin(
            hwnd, (bounds.left, bounds.top),
        )
        return {
            "hwnd": int(hwnd),
            "pid": int(pid),
            "process_started_at": float(process_started_at or 0.0),
            "window_class": str(class_name or ""),
            "window_title": str(title or ""),
            "window_bounds": (
                bounds.left, bounds.top, bounds.right, bounds.bottom,
            ),
            "client_bounds": (
                0,
                0,
                max(1, client.width if client is not None else bounds.width),
                max(1, client.height if client is not None else bounds.height),
            ),
            "client_origin_screen": tuple(client_origin),
            "dpi_scale": float(bounds.dpi_scale),
            "capture_protection": str(
                desktop_scene._window_capture_protection_reason(hwnd) or ""
            ),
            "minimized": minimized,
            "foreground_hwnd": int(
                (desktop_control._foreground_window_win32() or (0,))[0] or 0
            ),
            "bounds_object": bounds,
        }
    except Exception:
        return None


def _default_capture_window(hwnd: int, state: Mapping[str, Any]):
    if sys.platform != "win32":
        raise ComputerUseV4Error("CAPTURE_UNSUPPORTED", "exact_window_requires_win32")
    from osia import desktop_scene

    return desktop_scene._capture_exact_window_image(
        int(hwnd), state["bounds_object"],
    )


def _default_foreground_activation(
    frame: ComputerFrame,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Use OSIA's existing Win32 focus primitive once and verify the HWND."""

    receipt = {
        "attempted": True,
        "status": "failed",
        "target_hwnd": str(frame.hwnd),
        "target_pid": int(frame.pid),
        "before_foreground_hwnd": str(
            int(state.get("foreground_hwnd") or 0)
        ),
        "verified_foreground_hwnd": "",
        "reason_code": "target_foreground_activation_failed",
        "completed_at": time.time(),
    }
    if sys.platform != "win32":
        receipt["reason_code"] = "foreground_activation_unavailable"
        return receipt
    try:
        from osia.tools import desktop_control

        loaded = desktop_control._load_win32()
        if loaded is None:
            receipt["reason_code"] = "foreground_activation_unavailable"
            return receipt
        win32gui, win32process = loaded
        if not win32gui.IsWindow(frame.hwnd):
            receipt["reason_code"] = "target_window_missing"
            return receipt
        _thread_id, current_pid = win32process.GetWindowThreadProcessId(
            frame.hwnd,
        )
        if int(current_pid or 0) != frame.pid:
            receipt["reason_code"] = "window_identity_changed"
            return receipt
        desktop_control._raise_window_win32(
            win32gui, win32process, frame.hwnd,
        )
        foreground = desktop_control._foreground_window_win32()
    except Exception as exc:
        receipt["reason_code"] = type(exc).__name__
        receipt["completed_at"] = time.time()
        return receipt
    receipt["completed_at"] = time.time()
    if foreground is None:
        receipt["reason_code"] = "foreground_verification_unavailable"
        return receipt
    foreground_hwnd, _title, foreground_pid = foreground
    receipt["verified_foreground_hwnd"] = str(int(foreground_hwnd or 0))
    if (
        int(foreground_hwnd or 0) != frame.hwnd
        or int(foreground_pid or 0) != frame.pid
    ):
        return receipt
    receipt["status"] = "verified"
    receipt["reason_code"] = "target_foreground_verified"
    return receipt


class _PersistentV4CaptureSource:
    """Own exactly one persistent WGC session for one V4 loop target."""

    def __init__(self) -> None:
        self._hwnd = 0
        self._session = None
        self._last_snapshot: dict[str, Any] = {}

    def __call__(self, hwnd: int, state: Mapping[str, Any]):
        from osia.desktop_scene import PersistentWgcWindowCapture

        target = int(hwnd)
        if self._session is not None and self._hwnd != target:
            self.close()
        if self._session is None:
            self._session = PersistentWgcWindowCapture(target)
            self._hwnd = target
        return self._session.snapshot(state["bounds_object"])

    def snapshot(self) -> dict[str, Any]:
        if self._session is None:
            if self._last_snapshot:
                return dict(self._last_snapshot)
            return {
                "capture_start_count": 0,
                "capture_stop_count": 0,
                "frame_sequence": 0,
                "latest_frame_retained": False,
                "closed": False,
            }
        return dict(self._session.lifecycle_snapshot())

    def close(self) -> None:
        session = self._session
        if session is not None:
            session.close()
            self._last_snapshot = dict(session.lifecycle_snapshot())
            self._session = None
            self._hwnd = 0


def _window_token(state: Mapping[str, Any]) -> str:
    material = "|".join((
        str(state.get("pid") or ""),
        f"{float(state.get('process_started_at') or 0.0):.6f}",
        str(state.get("hwnd") or ""),
        str(state.get("window_class") or ""),
    ))
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]


class ComputerUseV4:
    """One isolated observe/action service bound to one provider/model turn."""

    def __init__(
        self,
        provider: str,
        model: str,
        turn_id: str,
        *,
        loop_id: str = "",
        frame_ttl_seconds: float = _DEFAULT_FRAME_TTL_SECONDS,
        maximum_image_dimension: int = _DEFAULT_MAX_IMAGE_DIMENSION,
        window_state: Callable[[int], Mapping[str, Any] | None] | None = None,
        capture_window: Callable[[int, Mapping[str, Any]], Any] | None = None,
        foreground_activation: Callable[
            [ComputerFrame, Mapping[str, Any]], Any
        ] | None = None,
        permission_check: Callable[[ComputerAction, ComputerFrame], Any] | None = None,
        action_store=None,
        dispatchers: Mapping[str, Callable[[ComputerAction, tuple[int, int] | None], Any]] | None = None,
        external_action_boundary: Callable[[ComputerAction, ComputerFrame, int], Any] | None = None,
        dispatch_result_verifier: Callable[[Any, ComputerAction, ComputerFrame], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.provider = str(provider or "").strip().lower()
        self.model = str(model or "").strip()
        self.turn_id = str(turn_id or "").strip()
        if not self.provider or not self.model or not self.turn_id:
            raise ValueError("computer_loop_route_required")
        self.loop_id = str(loop_id or "v4loop_" + secrets.token_urlsafe(12))
        self.frame_ttl_seconds = max(5.0, float(frame_ttl_seconds))
        self.maximum_image_dimension = max(256, int(maximum_image_dimension))
        self._window_state = window_state or _default_window_state
        self._capture_owner = None
        if capture_window is None and sys.platform == "win32":
            self._capture_owner = _PersistentV4CaptureSource()
            self._capture_window = self._capture_owner
        else:
            self._capture_window = capture_window or _default_capture_window
        self._foreground_activation = (
            foreground_activation or _default_foreground_activation
        )
        self._permission_check = permission_check
        if action_store is None:
            from osia.tool_attempts import get_tool_attempt_store

            action_store = get_tool_attempt_store()
        self.action_store = action_store
        self._dispatchers = dict(dispatchers or {})
        self._external_action_boundary = external_action_boundary
        self._dispatch_result_verifier = dispatch_result_verifier
        self._clock = clock
        self._frames: dict[str, ComputerFrame] = {}
        self._latest_frame_id = ""
        self._observation_required = False
        self._last_capture_lifecycle: dict[str, Any] = {}

    @property
    def latest_frame_id(self) -> str:
        return self._latest_frame_id

    @property
    def observation_required(self) -> bool:
        return self._observation_required

    def frame(self, frame_id: str) -> ComputerFrame | None:
        return self._frames.get(str(frame_id or ""))

    def capture_lifecycle_snapshot(self) -> dict[str, Any]:
        owner = self._capture_owner
        return (
            owner.snapshot()
            if owner is not None else dict(self._last_capture_lifecycle)
        )

    def close(self) -> None:
        owner = self._capture_owner
        self._capture_owner = None
        if owner is not None:
            owner.close()
            self._last_capture_lifecycle = owner.snapshot()

    def _capture(
        self, hwnd: int, *, expected_window_token: str = "",
    ) -> tuple[dict[str, Any], Any, str, dict]:
        state = dict(self._window_state(int(hwnd)) or {})
        if not state or int(state.get("hwnd") or 0) != int(hwnd):
            raise ComputerUseV4Error("WRONG_HWND", "target_window_missing")
        if state.get("capture_protection"):
            raise ComputerUseV4Error(
                "CAPTURE_PROTECTED", str(state["capture_protection"]),
            )
        if state.get("minimized"):
            raise ComputerUseV4Error("WINDOW_MINIMIZED", "target_window_is_minimized")
        if expected_window_token and _window_token(state) != expected_window_token:
            raise ComputerUseV4Error("STALE_WINDOW", "capture_target_identity_changed")
        try:
            image, method, provenance = self._capture_window(int(hwnd), state)
        except ComputerUseV4Error:
            raise
        except Exception as exc:
            code = str(exc or "")
            if code == "target_window_is_minimized":
                raise ComputerUseV4Error("WINDOW_MINIMIZED", code) from exc
            if "capture" in code.lower() and "protect" in code.lower():
                raise ComputerUseV4Error("CAPTURE_PROTECTED", code) from exc
            raise ComputerUseV4Error(
                "CAPTURE_FAILED", type(exc).__name__,
            ) from exc
        if image is None or image.width <= 0 or image.height <= 0:
            raise ComputerUseV4Error("CAPTURE_FAILED", "empty_exact_window_frame")
        if expected_window_token:
            current = dict(self._window_state(int(hwnd)) or {})
            if current.get("minimized"):
                raise ComputerUseV4Error("WINDOW_MINIMIZED", "target_window_is_minimized")
            if current.get("capture_protection"):
                raise ComputerUseV4Error("CAPTURE_PROTECTED", str(current["capture_protection"]))
            if _window_token(current) != expected_window_token:
                raise ComputerUseV4Error("STALE_WINDOW", "capture_target_identity_changed")
        return state, image.convert("RGB"), str(method or ""), dict(provenance or {})

    def screenshot(self, hwnd: int, *, expected_window_token: str = "") -> ComputerFrame:
        """Capture one exact window and make it the sole current authority."""

        state, image, method, provenance = self._capture(
            int(hwnd), expected_window_token=expected_window_token,
        )
        raw_capture_bounds = provenance.get("capture_screen_bounds")
        if not (
            isinstance(raw_capture_bounds, (list, tuple))
            and len(raw_capture_bounds) == 4
        ):
            raise ComputerUseV4Error(
                "CAPTURE_FAILED", "capture_screen_bounds_unavailable",
            )
        capture_bounds = tuple(int(value) for value in raw_capture_bounds)
        if capture_bounds[2] <= capture_bounds[0] or capture_bounds[3] <= capture_bounds[1]:
            raise ComputerUseV4Error("CAPTURE_FAILED", "invalid_capture_geometry")
        captured_at = float(provenance.get("captured_at") or self._clock())
        foreground_hwnd = int(state.get("foreground_hwnd") or 0)
        png, image_width, image_height = _frame_png(
            image, self.maximum_image_dimension,
        )
        frame = ComputerFrame(
            frame_id="v4frame_" + secrets.token_urlsafe(15),
            loop_id=self.loop_id,
            turn_id=self.turn_id,
            provider=self.provider,
            model=self.model,
            hwnd=int(hwnd),
            pid=int(state.get("pid") or 0),
            process_started_at=float(state.get("process_started_at") or 0.0),
            window_class=str(state.get("window_class") or "")[:160],
            window_title=str(state.get("window_title") or "")[:240],
            window_token=_window_token(state),
            captured_at=captured_at,
            expires_at=captured_at + self.frame_ttl_seconds,
            original_width=int(image.width),
            original_height=int(image.height),
            image_width=image_width,
            image_height=image_height,
            source_box=(0.0, 0.0, float(image.width), float(image.height)),
            capture_screen_bounds=capture_bounds,
            window_bounds=tuple(int(value) for value in state["window_bounds"]),
            client_bounds=tuple(int(value) for value in state["client_bounds"]),
            client_origin_screen=tuple(
                int(value) for value in state["client_origin_screen"]
            ),
            dpi_scale=float(state.get("dpi_scale") or 1.0),
            image_bytes=png,
            scene_sha256=_client_scene_fingerprint(image, state, provenance),
            image_sha256=hashlib.sha256(png).hexdigest(),
            capture_method=method,
            foreground_at_capture=foreground_hwnd == int(hwnd) if foreground_hwnd else None,
            capture_attempts=tuple(
                dict(item) for item in provenance.get("capture_attempts", ())
                if isinstance(item, Mapping)
            )[:8],
        )
        self._frames[frame.frame_id] = frame
        self._latest_frame_id = frame.frame_id
        self._observation_required = False
        while len(self._frames) > 8:
            oldest = next(iter(self._frames))
            if oldest == self._latest_frame_id:
                break
            self._frames.pop(oldest, None)
        return frame

    def crop(
        self,
        frame_id: str,
        left: float,
        top: float,
        right: float,
        bottom: float,
    ) -> ComputerFrame:
        """Create one model-visible crop while retaining the original mapping."""

        frame = self._frames.get(str(frame_id or ""))
        if frame is None or frame.frame_id != self._latest_frame_id:
            raise ComputerUseV4Error("STALE_FRAME", "crop_source_not_current")
        values = tuple(float(value) for value in (left, top, right, bottom))
        if not all(math.isfinite(value) for value in values):
            raise ComputerUseV4Error("OUT_OF_BOUNDS", "non_finite_crop")
        left, top, right, bottom = values
        if not (
            0.0 <= left < right <= float(frame.image_width)
            and 0.0 <= top < bottom <= float(frame.image_height)
        ):
            raise ComputerUseV4Error("OUT_OF_BOUNDS", "crop_outside_frame")
        from PIL import Image

        with Image.open(io.BytesIO(frame.image_bytes)) as opened:
            opened.load()
            pixel_box = (
                int(math.floor(left)), int(math.floor(top)),
                int(math.ceil(right)), int(math.ceil(bottom)),
            )
            cropped = opened.convert("RGB").crop(pixel_box)
        png, image_width, image_height = _frame_png(
            cropped, self.maximum_image_dimension,
        )
        source_left, source_top, source_right, source_bottom = frame.source_box
        source_width = source_right - source_left
        source_height = source_bottom - source_top
        source_box = (
            source_left + (pixel_box[0] / frame.image_width) * source_width,
            source_top + (pixel_box[1] / frame.image_height) * source_height,
            source_left + (pixel_box[2] / frame.image_width) * source_width,
            source_top + (pixel_box[3] / frame.image_height) * source_height,
        )
        cropped_frame = replace(
            frame,
            frame_id="v4frame_" + secrets.token_urlsafe(15),
            captured_at=self._clock(),
            expires_at=self._clock() + self.frame_ttl_seconds,
            image_width=image_width,
            image_height=image_height,
            source_box=source_box,
            image_bytes=png,
            image_sha256=hashlib.sha256(png).hexdigest(),
            capture_method="v4_crop",
            parent_frame_id=frame.frame_id,
        )
        self._frames[cropped_frame.frame_id] = cropped_frame
        self._latest_frame_id = cropped_frame.frame_id
        return cropped_frame

    def zoom(
        self,
        frame_id: str,
        left: float,
        top: float,
        right: float,
        bottom: float,
    ) -> ComputerFrame:
        """Model-facing read-only alias for a bounded crop observation."""

        return self.crop(frame_id, left, top, right, bottom)

    @staticmethod
    def _same_geometry(frame: ComputerFrame, state: Mapping[str, Any]) -> bool:
        return bool(
            tuple(int(value) for value in state.get("window_bounds", ()))
            == frame.window_bounds
            and tuple(int(value) for value in state.get("client_bounds", ()))
            == frame.client_bounds
            and tuple(int(value) for value in state.get("client_origin_screen", ()))
            == frame.client_origin_screen
        )

    def _guard(
        self,
        frame: ComputerFrame,
        *,
        action: ComputerAction,
        target_hwnd: int | None,
        verify_target: bool,
        allow_foreground_preparation: bool = True,
    ) -> tuple[str, str, dict[str, Any]]:
        preparation = {
            "attempted": False,
            "status": "not_required",
            "target_hwnd": str(frame.hwnd),
            "target_pid": int(frame.pid),
            "before_foreground_hwnd": "",
            "verified_foreground_hwnd": "",
            "reason_code": "target_already_foreground",
        }
        if frame.loop_id != self.loop_id or frame.turn_id != self.turn_id:
            return "STALE_FRAME", "foreign_loop_or_turn", preparation
        if frame.provider != self.provider or frame.model != self.model:
            return "STALE_FRAME", "provider_model_binding_changed", preparation
        if frame.frame_id != self._latest_frame_id:
            return "STALE_FRAME", "frame_superseded", preparation
        if self._clock() > frame.expires_at:
            return "STALE_FRAME", "frame_expired", preparation
        if target_hwnd is not None and int(target_hwnd) != frame.hwnd:
            return "STALE_FRAME", "action_window_mismatch", preparation

        def validated_state() -> tuple[str, str, dict[str, Any]]:
            candidate = dict(self._window_state(frame.hwnd) or {})
            if not candidate or int(candidate.get("hwnd") or 0) != frame.hwnd:
                return "STALE_FRAME", "target_window_missing", candidate
            if (
                int(candidate.get("pid") or 0) != frame.pid
                or str(candidate.get("window_class") or "")
                != frame.window_class
                or str(candidate.get("window_title") or "")
                != frame.window_title
                or _window_token(candidate) != frame.window_token
            ):
                return "STALE_FRAME", "window_identity_changed", candidate
            if candidate.get("capture_protection"):
                return (
                    "CAPTURE_PROTECTED",
                    str(candidate["capture_protection"]),
                    candidate,
                )
            if not self._same_geometry(frame, candidate):
                return "STALE_FRAME", "window_geometry_changed", candidate
            return "READY", "window_identity_verified", candidate

        state_status, state_reason, state = validated_state()
        if state_status != "READY":
            return state_status, state_reason, preparation
        foreground_hwnd = int(state.get("foreground_hwnd") or 0)
        preparation["before_foreground_hwnd"] = str(foreground_hwnd)
        if foreground_hwnd != frame.hwnd:
            if not allow_foreground_preparation:
                preparation.update({
                    "attempted": False,
                    "status": "failed",
                    "reason_code": "batch_target_foreground_changed",
                })
                return (
                    "FOREGROUND_PREPARATION_FAILED",
                    "batch_target_foreground_changed",
                    preparation,
                )
            try:
                activation = self._foreground_activation(frame, state)
            except Exception as exc:
                activation = {
                    "attempted": True,
                    "status": "failed",
                    "reason_code": type(exc).__name__,
                    "completed_at": time.time(),
                }
            preparation = (
                dict(activation) if isinstance(activation, Mapping) else {}
            )
            preparation.setdefault("attempted", True)
            preparation.setdefault("target_hwnd", str(frame.hwnd))
            preparation.setdefault("target_pid", int(frame.pid))
            preparation.setdefault(
                "before_foreground_hwnd", str(foreground_hwnd),
            )
            state_status, state_reason, state = validated_state()
            if state_status != "READY":
                return state_status, state_reason, preparation
            verified_foreground = int(state.get("foreground_hwnd") or 0)
            preparation["verified_foreground_hwnd"] = str(
                verified_foreground
            )
            if (
                preparation.get("status") != "verified"
                or verified_foreground != frame.hwnd
            ):
                preparation["status"] = "failed"
                if not preparation.get("reason_code"):
                    preparation["reason_code"] = (
                        "target_foreground_activation_failed"
                    )
                return (
                    "FOREGROUND_PREPARATION_FAILED",
                    str(preparation.get("reason_code") or (
                        "target_foreground_activation_failed"
                    )),
                    preparation,
                )
        if (
            verify_target
            and action.kind in {"click", "double_click"}
            and isinstance(action.x, (int, float))
            and not isinstance(action.x, bool)
            and isinstance(action.y, (int, float))
            and not isinstance(action.y, bool)
            and math.isfinite(float(action.x))
            and math.isfinite(float(action.y))
            and 0.0 <= float(action.x) < float(frame.image_width)
            and 0.0 <= float(action.y) < float(frame.image_height)
        ):
            try:
                current, _method, current_provenance = self._capture_window(
                    frame.hwnd, state,
                )
                current_bounds = current_provenance.get(
                    "capture_screen_bounds",
                )
            except Exception as exc:
                return "STALE_FRAME", type(exc).__name__, preparation
            if not (
                isinstance(current_bounds, (list, tuple))
                and len(current_bounds) == 4
                and tuple(int(value) for value in current_bounds)
                == frame.capture_screen_bounds
            ):
                return "STALE_FRAME", "capture_geometry_changed", preparation
            try:
                valid, reason = _target_structure_validity(
                    frame,
                    current,
                    x=float(action.x),
                    y=float(action.y),
                )
            except Exception as exc:
                return "STALE_FRAME", type(exc).__name__, preparation
            if not valid:
                return "STALE_FRAME", reason, preparation
        return "READY", "fresh_frame", preparation

    def _permission(self, action: ComputerAction, frame: ComputerFrame) -> tuple[bool, str]:
        if not callable(self._permission_check):
            return False, "permission_authority_unbound"
        try:
            decision = self._permission_check(action, frame)
        except Exception as exc:
            return False, type(exc).__name__
        if isinstance(decision, tuple):
            allowed = bool(decision[0]) if decision else False
            reason = str(decision[1] if len(decision) > 1 else "")
            return allowed, reason[:120]
        return bool(decision), "permission_valid" if decision else "permission_denied"

    def _action_identity(
        self,
        action: ComputerAction,
        frame: ComputerFrame,
        index: int,
    ) -> tuple[str, str, dict[str, Any]]:
        from osia.action_truth import action_identity, semantic_effect_identity

        safe_arguments = action.safe_arguments()
        target = {
            "kind": "desktop",
            "application": frame.window_token,
            "surface_ref": "v4:" + frame.window_token,
            "frame_id": frame.frame_id,
            "native_window_id": str(frame.hwnd),
            "conflict_scope": "v4:" + frame.window_token,
        }
        attempted_effect = action.effect_id or hashlib.sha256(
            repr(sorted(safe_arguments.items())).encode("utf-8", errors="replace")
        ).hexdigest()[:24]
        semantic_effect_id = semantic_effect_identity(
            turn_id=self.turn_id,
            semantic_node_id=f"v4_{frame.frame_id}_{index}",
            attempted_effect=attempted_effect,
            operation=f"computer_{action.kind}",
            target=target,
        )
        durable_action_id = action_identity(
            run_id=self.loop_id,
            turn_id=self.turn_id,
            semantic_effect_id=semantic_effect_id,
        )
        return durable_action_id, semantic_effect_id, target

    def _prepare_action_truth(
        self,
        action: ComputerAction,
        frame: ComputerFrame,
        index: int,
    ) -> tuple[dict[str, Any] | None, str]:
        action_id, semantic_effect_id, target = self._action_identity(
            action, frame, index,
        )
        safe_arguments = action.safe_arguments()
        attempt = self.action_store.create_attempt(
            conversation_id=self.loop_id,
            turn_id=self.turn_id,
            request_id=f"v4request_{frame.frame_id}_{index}",
            tool_call_id="v4call_" + secrets.token_urlsafe(12),
            tool_name=f"computer_{action.kind}",
            provider_id=self.provider,
            model_id=self.model,
            turn_context={"computer_use_version": 4},
            argument_fingerprint=self.action_store.fingerprint_arguments(
                safe_arguments,
            ),
            side_effect="reversible_local",
            idempotence="non_idempotent",
            retry_policy="never_after_dispatch",
            action_binding={
                "action_id": action_id,
                "semantic_effect_id": semantic_effect_id,
                "semantic_node_id": f"v4_{frame.frame_id}_{index}",
                "attempted_effect": action.effect_id or semantic_effect_id,
                "effect_role": "semantic",
                "operation": f"computer_{action.kind}",
                "target": target,
                "conflict_scope": "v4:" + frame.window_token,
            },
        )
        attempt_id = str(attempt["tool_attempt_id"])
        accepted = self.action_store.mark_accepted(attempt_id)
        self.action_store.arm_owner_dispatch_boundary(attempt_id)
        running = self.action_store.mark_running(accepted["tool_attempt_id"])
        return running, "actiontruth_prepared"

    def _prepare_dispatch(
        self,
        action: ComputerAction,
        point: tuple[int, int] | None,
    ) -> tuple[Callable[[], Any] | None, str]:
        injected = self._dispatchers.get(action.kind)
        if callable(injected):
            return lambda: injected(action, point), "mock_or_bound_dispatcher_ready"
        from osia.tools import desktop_control

        if action.kind in {"click", "double_click"}:
            assert point is not None
            prepared = desktop_control.prepare_click(
                point[0], point[1], double=action.kind == "double_click",
            )
            if isinstance(prepared, str) and prepared.upper().startswith("ERROR:"):
                return None, prepared[:160]
            return (
                lambda: desktop_control.dispatch_prepared_click(prepared),
                "desktop_click_ready",
            )
        if action.kind == "type":
            return lambda: desktop_control.type_text(action.text), "desktop_type_ready"
        if action.kind == "keypress":
            return lambda: desktop_control.press_key(action.key), "desktop_key_ready"
        if action.kind == "scroll":
            return (
                lambda: desktop_control.scroll(action.direction, action.amount),
                "desktop_scroll_ready",
            )
        return None, "unsupported_dispatcher"

    @staticmethod
    def _result(
        status: str,
        reason: str,
        *,
        action: ComputerAction | None = None,
        frame: ComputerFrame | None = None,
        point: tuple[int, int] | None = None,
        attempt: Mapping[str, Any] | None = None,
        dispatch_count: int = 0,
        foreground_preparation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status == "DISPATCHED_VERIFIED":
            text = (
                "Computer Use V4 received an authoritative effect receipt. "
                "Capture a new screenshot before choosing the next visual action."
            )
        elif status == "DISPATCHED":
            text = (
            "ERROR: Computer Use V4 dispatched the requested local effect once; "
            "its outcome is not verified. Capture a new screenshot before any "
            "further unbatched mutation."
            )
        else:
            text = f"ERROR: Computer Use V4 {status}: {reason}."
        truth = dict((attempt or {}).get("action_truth") or {})
        return {
            "status": status,
            "reason_code": reason,
            "text": text,
            "frame_id": frame.frame_id if frame is not None else "",
            "action_kind": action.kind if action is not None else "",
            "screen_point": list(point) if point is not None else [],
            "dispatch_count": int(dispatch_count),
            "attempt_id": str((attempt or {}).get("tool_attempt_id") or ""),
            "action_id": str(truth.get("action_id") or ""),
            "semantic_effect_id": str(truth.get("semantic_effect_id") or ""),
            "dispatch_state": str((attempt or {}).get("dispatch_state") or "not_dispatched"),
            "foreground_preparation": dict(
                foreground_preparation or {}
            ),
        }

    def _execute_one(
        self,
        action: ComputerAction,
        *,
        index: int,
        verify_target: bool,
        allow_pending_batch: bool,
    ) -> dict[str, Any]:
        frame = self._frames.get(action.frame_id)
        if frame is None:
            return self._result("STALE_FRAME", "frame_missing", action=action)
        point = None
        if action.kind in {"click", "double_click"}:
            try:
                point = frame.map_image_point_to_screen(action.x, action.y)
            except ComputerUseV4Error as exc:
                return self._result(
                    exc.code, exc.detail or exc.code,
                    action=action, frame=frame,
                )
        guard_status, guard_reason, foreground_preparation = self._guard(
            frame,
            action=action,
            target_hwnd=action.target_hwnd,
            verify_target=verify_target,
            allow_foreground_preparation=not allow_pending_batch,
        )
        if guard_status != "READY":
            return self._result(
                guard_status, guard_reason, action=action, frame=frame,
                foreground_preparation=foreground_preparation,
            )
        allowed, permission_reason = self._permission(action, frame)
        if not allowed:
            return self._result(
                "PERMISSION_DENIED", permission_reason,
                action=action, frame=frame,
                foreground_preparation=foreground_preparation,
            )
        post_permission_state = dict(
            self._window_state(frame.hwnd) or {}
        )
        post_permission_status = "READY"
        post_permission_reason = "target_foreground_verified"
        if (
            not post_permission_state
            or int(post_permission_state.get("hwnd") or 0) != frame.hwnd
        ):
            post_permission_status = "STALE_FRAME"
            post_permission_reason = "target_window_missing"
        elif (
            int(post_permission_state.get("pid") or 0) != frame.pid
            or str(post_permission_state.get("window_class") or "")
            != frame.window_class
            or str(post_permission_state.get("window_title") or "")
            != frame.window_title
            or _window_token(post_permission_state) != frame.window_token
        ):
            post_permission_status = "STALE_FRAME"
            post_permission_reason = "window_identity_changed"
        elif post_permission_state.get("capture_protection"):
            post_permission_status = "CAPTURE_PROTECTED"
            post_permission_reason = str(
                post_permission_state["capture_protection"]
            )
        elif not self._same_geometry(frame, post_permission_state):
            post_permission_status = "STALE_FRAME"
            post_permission_reason = "window_geometry_changed"
        elif int(
            post_permission_state.get("foreground_hwnd") or 0
        ) != frame.hwnd:
            post_permission_status = "FOREGROUND_PREPARATION_FAILED"
            post_permission_reason = "target_foreground_lost_after_permission"
        if post_permission_status != "READY":
            return self._result(
                post_permission_status,
                post_permission_reason,
                action=action,
                frame=frame,
                foreground_preparation=foreground_preparation,
            )
        dispatcher, preparation_reason = self._prepare_dispatch(action, point)
        if dispatcher is None:
            return self._result(
                "FAILED_BEFORE_EFFECT", preparation_reason,
                action=action, frame=frame, point=point,
                foreground_preparation=foreground_preparation,
            )
        external_boundary = callable(self._external_action_boundary)
        if external_boundary:
            boundary_result = self._external_action_boundary(
                action, frame, index,
            )
            if isinstance(boundary_result, tuple):
                attempt = boundary_result[0] if boundary_result else None
                truth_reason = str(
                    boundary_result[1] if len(boundary_result) > 1 else ""
                )
            else:
                attempt = boundary_result
                truth_reason = "external_action_boundary"
            if not isinstance(attempt, Mapping):
                return self._result(
                    "ACTION_FAILED",
                    truth_reason or "actiontruth_dispatch_claim_rejected",
                    action=action, frame=frame, point=point,
                    foreground_preparation=foreground_preparation,
                )
            attempt = dict(attempt)
            attempt_id = str(attempt.get("tool_attempt_id") or "")
            if not attempt_id:
                return self._result(
                    "ACTION_FAILED", "external_actiontruth_identity_missing",
                    action=action, frame=frame, point=point,
                    foreground_preparation=foreground_preparation,
                )
            started = attempt
        else:
            attempt, truth_reason = self._prepare_action_truth(
                action, frame, index,
            )
            if attempt is None:
                return self._result(
                    "ACTION_FAILED", truth_reason,
                    action=action, frame=frame, point=point,
                    foreground_preparation=foreground_preparation,
                )
            attempt_id = str(attempt["tool_attempt_id"])
            started, claimed = self.action_store.claim_owner_dispatch(attempt_id)
            if not claimed:
                return self._result(
                    "ACTION_FAILED", "actiontruth_dispatch_claim_rejected",
                    action=action, frame=frame, point=point, attempt=started,
                    foreground_preparation=foreground_preparation,
                )
        try:
            dispatch_result = dispatcher()
            dispatch_error = (
                isinstance(dispatch_result, str)
                and dispatch_result.upper().startswith("ERROR:")
            )
        except Exception:
            dispatch_result = None
            dispatch_error = True
        from osia.tool_attempts import ToolAttemptStatus

        verified = False
        if not dispatch_error and callable(self._dispatch_result_verifier):
            try:
                verified = bool(
                    self._dispatch_result_verifier(
                        dispatch_result, action, frame,
                    )
                )
            except Exception:
                verified = False
        if external_boundary:
            primitive_identity = {}
            journal = [
                dict(item) for item in attempt.get("primitive_journal") or []
                if isinstance(item, Mapping)
            ]
            if 0 <= index < len(journal):
                entry = journal[index]
                try:
                    terminal = self.action_store.record_primitive_dispatch_returned(
                        attempt_id,
                        primitive_id=str(entry.get("primitive_id") or ""),
                    )
                except Exception:
                    terminal = self.action_store.get_attempt(attempt_id) or started
                primitive_identity = {
                    "primitive_id": str(entry.get("primitive_id") or ""),
                    "action_id": str(entry.get("action_id") or ""),
                    "semantic_effect_id": str(
                        entry.get("semantic_effect_id") or ""
                    ),
                }
            else:
                terminal = self.action_store.get_attempt(attempt_id) or started
        else:
            primitive_identity = {}
            terminal, _created = self.action_store.record_terminal_result(
                attempt_id,
                status=(
                    ToolAttemptStatus.SUCCEEDED
                    if verified else ToolAttemptStatus.UNKNOWN_AFTER_START
                ),
                terminal_outcome=(
                    "success_verified" if verified else "outcome_unknown"
                ),
                dispatch_state=(
                    "effect_confirmed" if verified else "effect_unknown"
                ),
                reason_code=(
                    "v4_authoritative_effect_receipt"
                    if verified else
                    "v4_dispatch_returned_error"
                    if dispatch_error else "v4_post_action_observation_required"
                ),
            )
        self._observation_required = True
        output = self._result(
            (
                "DISPATCH_UNKNOWN" if dispatch_error else
                "DISPATCHED_VERIFIED" if verified else "DISPATCHED"
            ),
            (
                "dispatch_returned_error_outcome_unknown"
                if dispatch_error else
                "authoritative_effect_receipt"
                if verified else "post_action_screenshot_required"
            ),
            action=action,
            frame=frame,
            point=point,
            attempt=terminal,
            dispatch_count=1,
            foreground_preparation=foreground_preparation,
        )
        if primitive_identity:
            output.update(primitive_identity)
        return output

    def execute(self, action: ComputerAction | Mapping[str, Any]) -> dict[str, Any]:
        parsed = (
            action if isinstance(action, ComputerAction)
            else ComputerAction.from_mapping(action)
        )
        return self._execute_one(
            parsed, index=0, verify_target=True, allow_pending_batch=False,
        )

    def execute_batch(
        self,
        actions: Iterable[ComputerAction | Mapping[str, Any]],
    ) -> dict[str, Any]:
        parsed = [
            item if isinstance(item, ComputerAction)
            else ComputerAction.from_mapping(item)
            for item in actions
        ]
        if not 1 <= len(parsed) <= _MAX_BATCH_ACTIONS:
            raise ValueError("computer_batch_must_contain_one_to_three_actions")
        anchor = parsed[0].frame_id
        if any(item.frame_id != anchor for item in parsed):
            raise ValueError("computer_batch_requires_one_frame")
        results = []
        for index, action in enumerate(parsed):
            result = self._execute_one(
                action,
                index=index,
                verify_target=index == 0,
                allow_pending_batch=index > 0,
            )
            results.append(result)
            if result["status"] not in {"DISPATCHED", "DISPATCHED_VERIFIED"}:
                break
            if action.observe_after and index < len(parsed) - 1:
                results.append(self._result(
                    "REOBSERVE_REQUIRED",
                    "model_marked_observation_boundary",
                    action=parsed[index + 1],
                    frame=self._frames.get(parsed[index + 1].frame_id),
                ))
                break
        executed_count = sum(
            int(item.get("dispatch_count") or 0) for item in results
        )
        return {
            "status": (
                str(results[-1].get("status") or "DISPATCHED")
                if executed_count == len(parsed)
                else str(results[-1].get("status") or "BATCH_STOPPED")
            ),
            "reason_code": str(results[-1].get("reason_code") or ""),
            "results": results,
            "executed_count": executed_count,
            "observation_required": self._observation_required,
        }


__all__ = [
    "ComputerAction",
    "ComputerFrame",
    "ComputerUseV4",
    "ComputerUseV4Error",
    "computer_action_schema",
]
