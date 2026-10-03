"""Generic semantic observation and bounded capture for native desktop apps."""

from __future__ import annotations

import contextvars
import ctypes
import hashlib
import io
import json
import math
import re
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from osia.desktop_surface import (
    DESKTOP_SURFACE_REGISTRY,
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
)
from osia.desktop_vision_policy import (
    AccessibilityQuality,
    DesktopEvidence,
    classify_accessibility_quality,
)
from osia.evidence_arbitration import (
    EvidenceCoverage,
    EvidenceSource,
    observer_evidence_semantics,
)
from osia.image_inputs import image_input_decision
from osia.visual_observation import (
    CaptureKind,
    ObservationReason,
    VisualObservationStore,
)
from osia.vision_runtime import (
    CaptureFailure,
    ImageInputTransport,
    NormalizedBBox,
    VisionCaptureError,
    VisionDomain,
    VisionFailure,
    VisionRuntime,
    VisionStage,
    VisionTargetError,
    VisionTrace,
    VisionTransportError,
    VisualStatus,
    VisualTarget,
    WindowsCaptureDriver,
    crop_capture,
    visual_frame_pixels_unchanged,
)


_MAX_UIA_NODES = 320
_MAX_MODEL_SURFACES = 80
_ACTIONABLE_PATTERNS = frozenset({
    "ExpandCollapse", "Invoke", "SelectionItem", "Toggle", "Value",
})
_BACKGROUND_SAFE_UIA_PATTERNS = frozenset({
    "ExpandCollapse", "Invoke", "SelectionItem", "Toggle",
})
_UIA_ACTION_PATTERN_METHODS = (
    ("Invoke", "GetInvokePattern", "Invoke"),
    ("SelectionItem", "GetSelectionItemPattern", "Select"),
    ("Toggle", "GetTogglePattern", "Toggle"),
    ("ExpandCollapse", "GetExpandCollapsePattern", "Expand"),
)
_PRIMITIVE_STATE_ROLES = frozenset({
    "DocumentControl", "EditControl", "PaneControl", "TextControl",
})
_MAX_PRIMITIVE_STATE_ROWS = 32
_PRIMITIVE_ABSENCE_SETTLE_SECONDS = 5.0
_UIA_ACTION_DISPATCH_TIMEOUT_SECONDS = 0.75
_UIA_ACTION_CANCEL_SETTLE_SECONDS = 0.1
_SECURE_DESKTOP_PROCESS_NAMES = frozenset({
    "consent", "credentialuibroker", "lockapp", "logonui", "winlogon",
})
_SCENE_STORES: contextvars.ContextVar[VisualObservationStore | None] = (
    contextvars.ContextVar("osia_visual_observation_store", default=None)
)
_MODEL_ROUTE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "osia_desktop_model_route", default=None,
)
_LAST_SCENE: contextvars.ContextVar["DesktopScene | None"] = contextvars.ContextVar(
    "osia_last_desktop_scene", default=None,
)
_VISUAL_FRAMES: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "osia_visual_frames", default=None,
)
_ACTUATION_FRAME_LEASE_SECONDS = 30.0
_ACTUATION_FRAME_MAX_LIFETIME_SECONDS = 60.0
_ACTUATION_FRAME_LOCK = threading.RLock()
_ACTUATION_FRAMES: dict[str, dict] = {}


def _prune_visual_actuation_frames(*, now: float | None = None) -> None:
    moment = time.time() if now is None else float(now)
    stale = [
        frame_id for frame_id, entry in _ACTUATION_FRAMES.items()
        if float(entry.get("retain_until") or 0.0) < moment
    ]
    for frame_id in stale:
        _ACTUATION_FRAMES.pop(frame_id, None)


def register_visual_actuation_frame(frame) -> dict:
    """Register ephemeral pixels for a later acknowledged mutation proposal."""

    route = _MODEL_ROUTE.get() or {}
    retain_until = float(frame.captured_at) + (
        _ACTUATION_FRAME_MAX_LIFETIME_SECONDS
    )
    entry = {
        "frame": frame,
        "provider": str(frame.provider or ""),
        "model": str(frame.model or ""),
        "owner_id": str(route.get("owner_id") or ""),
        "turn_id": str(route.get("turn_id") or ""),
        "surface_ref": str(frame.surface_ref or ""),
        "window_generation": str(frame.window_generation or ""),
        "application": str(frame.application_id or ""),
        "acknowledged_at": 0.0,
        "lease_expires_at": 0.0,
        "retain_until": retain_until,
    }
    with _ACTUATION_FRAME_LOCK:
        _prune_visual_actuation_frames()
        _ACTUATION_FRAMES[str(frame.frame_id)] = entry
        while len(_ACTUATION_FRAMES) > 16:
            _ACTUATION_FRAMES.pop(next(iter(_ACTUATION_FRAMES)))
    return {
        "frame_id": str(frame.frame_id),
        "lease_state": "awaiting_provider_acknowledgement",
        "lease_expires_at": 0.0,
    }


def acknowledge_visual_actuation_frame(
    frame_id: str,
    *,
    provider: str,
    model: str,
    owner_id: str = "",
    turn_id: str = "",
    now: float | None = None,
) -> dict:
    """Create one bounded lease after correlated provider consumption."""

    moment = time.time() if now is None else float(now)
    route = _MODEL_ROUTE.get() or {}
    expected_owner = str(owner_id or route.get("owner_id") or "")
    expected_turn = str(turn_id or route.get("turn_id") or "")
    with _ACTUATION_FRAME_LOCK:
        _prune_visual_actuation_frames(now=moment)
        entry = _ACTUATION_FRAMES.get(str(frame_id or ""))
        if not isinstance(entry, dict):
            return {"lease_state": "unavailable", "lease_expires_at": 0.0}
        if (
            str(entry.get("provider") or "") != str(provider or "")
            or str(entry.get("model") or "") != str(model or "")
            or (
                expected_owner and str(entry.get("owner_id") or "")
                != expected_owner
            )
            or (
                expected_turn and str(entry.get("turn_id") or "")
                != expected_turn
            )
        ):
            return {"lease_state": "identity_mismatch", "lease_expires_at": 0.0}
        lease_expires_at = min(
            moment + _ACTUATION_FRAME_LEASE_SECONDS,
            float(entry.get("retain_until") or 0.0),
        )
        if lease_expires_at <= moment:
            return {"lease_state": "expired", "lease_expires_at": lease_expires_at}
        entry["acknowledged_at"] = moment
        entry["lease_expires_at"] = lease_expires_at
        return {
            "lease_state": "actuation_leased",
            "acknowledged_at": moment,
            "lease_expires_at": lease_expires_at,
        }


def _leased_visual_actuation_frame(
    frame_id: str,
    *,
    provider: str,
    model: str,
    now: float | None = None,
) -> tuple[object | None, str, dict]:
    moment = time.time() if now is None else float(now)
    route = _MODEL_ROUTE.get() or {}
    expected_owner = str(route.get("owner_id") or "")
    expected_turn = str(route.get("turn_id") or "")
    with _ACTUATION_FRAME_LOCK:
        _prune_visual_actuation_frames(now=moment)
        entry = _ACTUATION_FRAMES.get(str(frame_id or ""))
        if not isinstance(entry, dict):
            return None, "VISUAL_FRAME_UNLEASED", {}
        if (
            str(entry.get("provider") or "") != str(provider or "")
            or str(entry.get("model") or "") != str(model or "")
            or (
                expected_owner
                and str(entry.get("owner_id") or "") != expected_owner
            )
            or (
                expected_turn
                and str(entry.get("turn_id") or "") != expected_turn
            )
        ):
            return None, "VISUAL_FRAME_MODEL_MISMATCH", {}
        if float(entry.get("acknowledged_at") or 0.0) <= 0.0:
            return None, "VISUAL_FRAME_UNLEASED", dict(entry)
        lease_expires_at = float(entry.get("lease_expires_at") or 0.0)
        if lease_expires_at <= moment:
            return None, "VISUAL_FRAME_LEASE_EXPIRED", dict(entry)
        return entry.get("frame"), "", dict(entry)


def invalidate_visual_actuation_frames(
    *,
    surface_ref: str = "",
    window_generation: str = "",
) -> None:
    """Invalidate pre-transition localization for one exact surface lineage."""

    expected_surface = str(surface_ref or "")
    expected_generation = str(window_generation or "")
    with _ACTUATION_FRAME_LOCK:
        matching = [
            frame_id for frame_id, entry in _ACTUATION_FRAMES.items()
            if (not expected_surface or entry.get("surface_ref") == expected_surface)
            and (
                not expected_generation
                or entry.get("window_generation") == expected_generation
            )
        ]
        for frame_id in matching:
            _ACTUATION_FRAMES.pop(frame_id, None)


class SurfaceCoverage(str, Enum):
    FULL_WINDOW = "full_window"
    CLIENT_CONTENT = "client_content"
    REGION = "region"
    CHROME_ONLY = "chrome_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NativeWindowSurfaceCandidate:
    """Sanitized native hierarchy/geometry used for generic visual selection."""

    hwnd: int
    pid: int
    process_started_at: float
    class_name: str
    title: str
    parent_hwnd: int
    owner_hwnd: int
    root_hwnd: int
    is_window: bool
    visible: bool
    cloaked: bool
    bounds: SurfaceBounds | None
    client_bounds: SurfaceBounds | None
    monitor: str = ""
    dpi: int = 96
    surface_id: str = ""
    generation: str = ""
    uia_control_type: str = ""
    content_host_signal: bool = False
    tool_window: bool = False
    popup_window: bool = False
    foreground: bool = False
    stale: bool = False

    @property
    def pixel_area(self) -> int:
        bounds = self.client_bounds or self.bounds
        return int(bounds.width * bounds.height) if bounds is not None else 0

    def safe_dict(self, *, application_bounds: SurfaceBounds | None) -> dict:
        return {
            "hwnd": str(self.hwnd),
            "pid": int(self.pid),
            "process_start_identity": round(
                float(self.process_started_at), 6,
            ),
            "window_class": _safe_label(self.class_name),
            "title": _safe_label(self.title),
            "parent_hwnd": str(self.parent_hwnd or ""),
            "owner_hwnd": str(self.owner_hwnd or ""),
            "root_hwnd": str(self.root_hwnd or ""),
            "is_window": bool(self.is_window),
            "visible": bool(self.visible),
            "cloaked": bool(self.cloaked),
            "window_bounds": (
                self.bounds.to_safe_dict() if self.bounds is not None else {}
            ),
            "client_bounds": (
                self.client_bounds.to_safe_dict()
                if self.client_bounds is not None else {}
            ),
            "pixel_area": self.pixel_area,
            "monitor": str(self.monitor or "")[:160],
            "dpi": int(self.dpi),
            "surface_id": str(self.surface_id or "")[:180],
            "generation": str(self.generation or "")[:180],
            "uia_root_control_type": str(
                self.uia_control_type or ""
            )[:120],
            "contains_render_content_host": bool(self.content_host_signal),
            "tool_window": bool(self.tool_window),
            "popup_window": bool(self.popup_window),
            "foreground": bool(self.foreground),
            "coverage": _surface_candidate_coverage(
                self, application_bounds,
            ).value,
        }


@dataclass(frozen=True)
class VisualSurfaceSelection:
    capture: NativeWindowSurfaceCandidate | None
    content: NativeWindowSurfaceCandidate | None
    coverage: SurfaceCoverage
    candidate_count: int

    def safe_dict(
        self,
        candidates: Iterable[NativeWindowSurfaceCandidate],
        *,
        application_bounds: SurfaceBounds | None,
        captured_dimensions: dict | None = None,
    ) -> dict:
        bounded = tuple(candidates)[:80]
        captured_dimensions = (
            captured_dimensions if isinstance(captured_dimensions, dict) else {}
        )
        selected_root = (
            int(self.capture.root_hwnd or self.capture.hwnd)
            if self.capture is not None else 0
        )
        return {
            "schema_version": 1,
            "candidate_count": int(self.candidate_count),
            "same_root_candidate_count": sum(
                int(item.root_hwnd or item.hwnd) == selected_root
                for item in bounded
            ) if selected_root else 0,
            "selected_capture_hwnd": (
                str(self.capture.hwnd) if self.capture is not None else ""
            ),
            "selected_content_hwnd": (
                str(self.content.hwnd) if self.content is not None else ""
            ),
            "content_coverage": self.coverage.value,
            "captured_frame_dimensions": {
                "width": int(captured_dimensions.get("width") or 0),
                "height": int(captured_dimensions.get("height") or 0),
            },
            "candidates": [
                {
                    **item.safe_dict(application_bounds=application_bounds),
                    "capture_attempted": bool(
                        self.capture is not None
                        and item.hwnd == self.capture.hwnd
                    ),
                }
                for item in bounded
            ],
        }


def _surface_candidate_coverage(
    candidate: NativeWindowSurfaceCandidate,
    application_bounds: SurfaceBounds | None,
) -> SurfaceCoverage:
    bounds = candidate.client_bounds or candidate.bounds
    if bounds is None or application_bounds is None or not bounds.valid():
        return SurfaceCoverage.UNKNOWN
    app_area = max(1, application_bounds.width * application_bounds.height)
    area_ratio = (bounds.width * bounds.height) / float(app_area)
    width_ratio = bounds.width / float(max(1, application_bounds.width))
    height_ratio = bounds.height / float(max(1, application_bounds.height))
    if height_ratio <= 0.24 and width_ratio >= 0.55:
        return SurfaceCoverage.CHROME_ONLY
    if (
        candidate.hwnd == candidate.root_hwnd
        and area_ratio >= 0.65
        and width_ratio >= 0.75
        and height_ratio >= 0.65
    ):
        return SurfaceCoverage.FULL_WINDOW
    if (
        area_ratio >= 0.40
        and width_ratio >= 0.60
        and height_ratio >= 0.45
        and candidate.content_host_signal
    ):
        return SurfaceCoverage.CLIENT_CONTENT
    return SurfaceCoverage.REGION


def select_application_visual_surface(
    candidates: Iterable[NativeWindowSurfaceCandidate],
    semantic_hwnd: int,
) -> VisualSurfaceSelection:
    """Choose a composed application root plus its meaningful content host."""

    records = tuple(candidates)
    semantic = next(
        (item for item in records if int(item.hwnd) == int(semantic_hwnd)),
        None,
    )
    if semantic is None or not semantic.is_window:
        return VisualSurfaceSelection(
            None, None, SurfaceCoverage.UNKNOWN, len(records),
        )
    semantic_root = int(semantic.root_hwnd or semantic.hwnd)
    related = [
        item for item in records
        if item.is_window
        and not item.stale
        and (
            int(item.root_hwnd or item.hwnd) == semantic_root
            or int(item.hwnd) == semantic_root
        )
    ]
    roots = [
        item for item in related
        if int(item.hwnd) == semantic_root
        and item.visible
        and not item.cloaked
        and not item.tool_window
        and not item.popup_window
    ]
    capture = roots[0] if len(roots) == 1 else None
    if capture is None and semantic in related and not (
        semantic.tool_window or semantic.popup_window or semantic.cloaked
    ):
        capture = semantic
    if capture is None:
        return VisualSurfaceSelection(
            None, None, SurfaceCoverage.UNKNOWN, len(records),
        )
    application_bounds = capture.bounds or semantic.bounds
    content_candidates = [
        item for item in related
        if item.visible
        and not item.cloaked
        and not item.tool_window
        and not item.popup_window
        and _surface_candidate_coverage(
            item, application_bounds,
        ) in {
            SurfaceCoverage.FULL_WINDOW,
            SurfaceCoverage.CLIENT_CONTENT,
        }
    ]
    content_candidates.sort(key=lambda item: (
        item.content_host_signal,
        _surface_candidate_coverage(item, application_bounds)
        is SurfaceCoverage.CLIENT_CONTENT,
        item.hwnd == capture.hwnd,
        item.pixel_area,
    ), reverse=True)
    content = content_candidates[0] if content_candidates else capture
    capture_coverage = _surface_candidate_coverage(capture, application_bounds)
    content_coverage = _surface_candidate_coverage(content, application_bounds)
    coverage = (
        SurfaceCoverage.FULL_WINDOW
        if capture_coverage is SurfaceCoverage.FULL_WINDOW else
        content_coverage
    )
    return VisualSurfaceSelection(
        capture, content, coverage, len(records),
    )


@dataclass(frozen=True)
class DesktopScene:
    application_id: str
    window_ref: DesktopSurfaceRef
    surfaces: tuple[DesktopSurfaceRef, ...]
    structural_epoch: int
    scene_signature: str
    accessibility_complete: bool
    observed_at: float
    uia_tree_observed: bool = True
    foreground: bool = True
    host_application_id: str = ""
    content_application_id: str = ""
    packaged_application_id: str = ""
    minimized: bool = False

    def target_evidence(
        self,
        target: str = "",
        *,
        requested: DesktopSurfaceRef | None = None,
        scene_changed: bool = False,
    ) -> DesktopEvidence:
        candidates = rank_surface_candidates(self.surfaces, target)
        actionable_patterns = sum(
            bool(_ACTIONABLE_PATTERNS.intersection(item.patterns))
            for item in self.surfaces
            if item.surface_kind is not SurfaceKind.WINDOW
        )
        actionable_candidates = sum(
            bool(
                item.enabled
                and _ACTIONABLE_PATTERNS.intersection(item.patterns)
            )
            for item in candidates
        )
        return DesktopEvidence(
            candidate_count=len(candidates),
            actionable_candidate_count=actionable_candidates,
            accessible_node_count=max(0, len(self.surfaces) - 1),
            custom_rendered=bool(
                len(self.surfaces) > 1 and actionable_patterns == 0
            ),
            popup_suspected=any(
                item.surface_kind is SurfaceKind.DIALOG
                for item in self.surfaces
            ),
            small_target=bool(
                requested
                and requested.bounds
                and (
                    requested.bounds.width < 48
                    or requested.bounds.height < 28
                )
            ),
            local_region_available=bool(
                (requested and requested.bounds) or self.window_ref.bounds
            ),
            scene_changed=bool(scene_changed),
            target_requested=bool(str(target or "").strip()),
            actionable_pattern_count=actionable_patterns,
            focus_observable=any(
                item.focused and item.surface_kind is not SurfaceKind.WINDOW
                for item in self.surfaces
            ),
            tree_observed=self.uia_tree_observed,
            scene_window_exact=True,
        )

    def accessibility_quality(self, target: str = "") -> AccessibilityQuality:
        return classify_accessibility_quality(self.target_evidence(target))

    def safe_dict(self, *, target: str = "") -> dict:
        candidates = rank_surface_candidates(self.surfaces, target)
        evidence = self.target_evidence(target)
        quality = classify_accessibility_quality(evidence)
        by_ref = {item.ref_id: item for item in self.surfaces}
        projected: list[DesktopSurfaceRef] = []
        projected_refs: set[str] = set()

        def include_with_ancestors(item: DesktopSurfaceRef) -> None:
            parent = by_ref.get(item.parent_ref or "")
            if parent is not None and parent.ref_id not in projected_refs:
                include_with_ancestors(parent)
            if (
                item.ref_id not in projected_refs
                and len(projected) < _MAX_MODEL_SURFACES
            ):
                projected.append(item)
                projected_refs.add(item.ref_id)

        include_with_ancestors(self.window_ref)
        for item in candidates[:12]:
            include_with_ancestors(item)
        for item in self.surfaces:
            if item.selected or item.focused or item.surface_kind is SurfaceKind.DIALOG:
                include_with_ancestors(item)
        for item in self.surfaces:
            include_with_ancestors(item)
        coverage = (
            EvidenceCoverage.COMPLETE
            if self.accessibility_complete and self.uia_tree_observed else
            EvidenceCoverage.PARTIAL
            if self.uia_tree_observed else
            EvidenceCoverage.UNKNOWN
        )
        return {
            "application": self.application_id,
            "host_application": self.host_application_id or self.application_id,
            "content_application": self.content_application_id,
            "window_surface_ref": self.window_ref.ref_id,
            "window_generation": self.window_ref.window_generation,
            "target_foreground": bool(self.foreground),
            "target_minimized": bool(self.minimized),
            "structural_epoch": self.structural_epoch,
            "accessibility_complete": self.accessibility_complete,
            "accessibility_quality": quality.value,
            "evidence_semantics": observer_evidence_semantics(
                EvidenceSource.UIA,
                coverage,
                target_identity=(
                    f"{self.application_id}:{self.window_ref.window_generation}"
                ),
                surface_id=self.window_ref.ref_id,
            ),
            "target_candidate_count": evidence.candidate_count,
            "target_actionable_candidate_count": (
                evidence.actionable_candidate_count
            ),
            "focus_observable": evidence.focus_observable,
            "observed_at": round(self.observed_at, 3),
            "selected_views": [
                item.ref_id for item in self.surfaces
                if item.selected and item.surface_kind in {
                    SurfaceKind.TAB,
                    SurfaceKind.PANE,
                    SurfaceKind.CONVERSATION,
                    SurfaceKind.LIST,
                }
            ],
            "focused_surface_ref": next(
                (
                    item.ref_id for item in self.surfaces
                    if item.focused and item.surface_kind is not SurfaceKind.WINDOW
                ),
                self.window_ref.ref_id if self.window_ref.focused else None,
            ),
            "modal_surface_refs": [
                item.ref_id for item in self.surfaces
                if item.surface_kind is SurfaceKind.DIALOG
            ],
            "candidate_surface_refs": [item.ref_id for item in candidates[:12]],
            "surfaces": [
                {
                    **item.to_safe_dict(),
                    "actuation": _surface_actuation_contract(
                        item,
                        target_foreground=self.foreground,
                        target_minimized=self.minimized,
                    ),
                }
                for item in projected
            ],
        }


def _surface_actuation_contract(
    surface: DesktopSurfaceRef,
    *,
    target_foreground: bool,
    target_minimized: bool,
) -> dict:
    """Describe exact per-surface preconditions without application heuristics."""

    if surface.surface_kind in {SurfaceKind.WINDOW, SurfaceKind.DIALOG}:
        return {
            "available": True,
            "attempted_effect": "application_foreground",
            "preconditions": ["exact_target_exists", "target_restorable"],
            "background_safe": False,
            "foreground_required": False,
            "keyboard_focus_required": False,
        }
    safe_patterns = tuple(
        item for item in (
            "Invoke", "SelectionItem", "Toggle", "ExpandCollapse",
        )
        if item in surface.patterns
    )
    if safe_patterns and surface.enabled and not target_minimized:
        return {
            "available": True,
            "attempted_effect": "desktop_uia_primitive",
            "preconditions": [
                "exact_target_exists", "target_visible_not_minimized",
                "fresh_runtime_identity",
            ],
            "background_safe": True,
            "foreground_required": False,
            "keyboard_focus_required": False,
            "patterns": list(safe_patterns),
        }
    return {
        "available": False,
        "attempted_effect": "",
        "preconditions": [
            "exact_target_exists",
            *([] if target_foreground else ["target_foreground"]),
            *([] if not target_minimized else ["target_restored"]),
        ],
        "background_safe": False,
        "foreground_required": not target_foreground,
        "keyboard_focus_required": False,
        "reason_code": "safe_uia_pattern_unavailable",
    }


def bind_effective_model(
    provider: str,
    model: str,
    *,
    owner_id: str = "",
    turn_id: str = "",
):
    """Bind the exact route whose next provider request may receive an image."""

    return _MODEL_ROUTE.set({
        "provider": str(provider or "").lower(),
        "model": str(model or ""),
        "owner_id": str(owner_id or ""),
        "turn_id": str(turn_id or ""),
    })


def reset_effective_model(token) -> None:
    _MODEL_ROUTE.reset(token)


def begin_desktop_turn() -> tuple[object, ...]:
    return (
        _SCENE_STORES.set(VisualObservationStore()),
        _LAST_SCENE.set(None),
        _VISUAL_FRAMES.set({}),
    )


def end_desktop_turn(tokens: tuple[object, ...]) -> None:
    store = _SCENE_STORES.get()
    if store is not None:
        store.clear()
    frames = _VISUAL_FRAMES.get()
    if frames is not None:
        frames.clear()
    owner_id, turn_id = effective_model_media_scope()
    if owner_id and turn_id:
        try:
            from osia.image_inputs import (
                release_media_owner,
                turn_scoped_media_owner,
            )

            release_media_owner(
                turn_scoped_media_owner(owner_id, turn_id)
            )
        except Exception:
            pass
    store_token, scene_token, frame_token = tokens
    _VISUAL_FRAMES.reset(frame_token)
    _LAST_SCENE.reset(scene_token)
    _SCENE_STORES.reset(store_token)


def effective_model_supports_vision() -> bool:
    route = _MODEL_ROUTE.get() or {}
    return image_input_decision(
        route.get("provider", ""),
        route.get("model", ""),
        ("image/png",),
    ).usable


def effective_model_route_is_bound() -> bool:
    """Whether the current execution context identifies an exact model route."""

    return _MODEL_ROUTE.get() is not None


def effective_model_image_route() -> tuple[str, str]:
    """Return the exact bound route; never resolve a companion Vision model."""

    route = _MODEL_ROUTE.get() or {}
    provider = str(route.get("provider") or "").lower()
    model = str(route.get("model") or "")
    decision = image_input_decision(provider, model, ("image/png",))
    return (provider, model) if decision.usable else ("", "")


def effective_model_media_scope() -> tuple[str, str]:
    route = _MODEL_ROUTE.get() or {}
    return (
        str(route.get("owner_id") or ""),
        str(route.get("turn_id") or ""),
    )


def _safe_label(value: object, *, password: bool = False) -> str:
    if password:
        return "[sensitive field]"
    label = " ".join(str(value or "").split())
    return label[:240]


def _runtime_id(control) -> tuple[int, ...]:
    try:
        value = control.GetRuntimeId()
    except Exception:
        value = ()
    try:
        return tuple(int(item) for item in (value or ()))[:32]
    except Exception:
        return ()


def _runtime_signature(
    runtime_id: tuple[int, ...],
    role: str,
    ancestor: str,
    automation_id: str = "",
) -> str:
    if not runtime_id and not automation_id:
        return ""
    identity = f"automation:{automation_id}" if automation_id else repr(runtime_id)
    material = f"{identity}\x1f{role}\x1f{ancestor}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _process_details(pid: int) -> tuple[str, float]:
    try:
        import psutil

        process = psutil.Process(int(pid))
        return Path(process.name()).stem.lower(), float(process.create_time())
    except Exception:
        # Without a trustworthy process birth time, never claim stability across
        # observations: a fresh nonce-like timestamp makes prior refs stale.
        return f"process-{int(pid or 0)}", time.time()


def _semantic_application_id(
    window_application_id: str,
    window_pid: int,
    controls: Iterable[dict],
) -> str:
    """Resolve the real app behind the generic Windows UWP frame host."""

    if str(window_application_id or "").lower() != "applicationframehost":
        return str(window_application_id or "")
    process_counts: dict[int, int] = {}
    for item in controls:
        try:
            process_id = int(item.get("process_id") or 0)
        except (TypeError, ValueError):
            continue
        if process_id <= 0 or process_id == int(window_pid):
            continue
        process_counts[process_id] = process_counts.get(process_id, 0) + 1
    for process_id, _count in sorted(
        process_counts.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        candidate, _started_at = _process_details(process_id)
        if candidate and not candidate.startswith("process-"):
            return candidate
    return str(window_application_id or "")


def _application_identity(value: object) -> str:
    normalized = "".join(
        char for char in str(value or "").lower()
        if char.isalnum()
    )
    for suffix in ("application", "app", "exe"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 2:
            normalized = normalized[:-len(suffix)]
            break
    return normalized


def _application_words(value: object) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _scene_application_identity(
    scene: DesktopScene,
    expected: str,
) -> tuple[int, str]:
    """Return one structured target-identity rank and its bounded evidence."""

    target = _application_identity(expected)
    observed = _application_identity(scene.application_id)
    host = _application_identity(
        scene.host_application_id or scene.application_id,
    )
    if not target:
        return 0, ""
    content = _application_identity(scene.content_application_id)
    if host == "applicationframehost" and content == target:
        return 400, f"uia_content_application:{scene.content_application_id}"
    packaged_application_id = str(scene.packaged_application_id or "")
    if packaged_application_id:
        try:
            from osia.tools import desktop_control as dc

            packaged_match = bool(
                dc._packaged_app_identity_score(expected, packaged_application_id)
            )
        except Exception:
            packaged_match = False
        if packaged_match:
            return 350, (
                "packaged_application_id:"
                + _safe_label(packaged_application_id)
            )
    if observed == target:
        if host == "applicationframehost" and observed != host:
            return 400, f"uia_content_application:{scene.application_id}"
        return 400, f"executable_application:{scene.application_id}"
    # A generic UWP frame is never application identity by itself.  Only the
    # content-process identity projected above can turn it into an exact app.
    if host == "applicationframehost" or observed == "applicationframehost":
        return 0, ""
    title = scene.window_ref.accessible_label
    if _application_identity(title) == target:
        return 300, f"exact_window_title:{_safe_label(title)}"
    expected_words = _application_words(expected)
    title_words = _application_words(title)
    if not expected_words or len(expected_words) > len(title_words):
        return 0, ""
    width = len(expected_words)
    if any(
        title_words[index:index + width] == expected_words
        for index in range(len(title_words) - width + 1)
    ):
        return 200, f"structured_window_title:{_safe_label(title)}"
    return 0, ""


def scene_matches_application(scene: DesktopScene, expected: str) -> bool:
    """Match one scene to the intended app without trusting PID alone."""

    rank, _evidence = _scene_application_identity(scene, expected)
    return rank > 0


def _ensure_thread_physical_pixels() -> None:
    """Use the same physical-pixel coordinate space as UIA and ImageGrab."""

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.SetThreadDpiAwarenessContext(
            ctypes.c_void_p(-4),  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except Exception:
        pass


def _window_bounds(hwnd: object) -> SurfaceBounds | None:
    if sys.platform != "win32":
        return None
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return None
        win32gui, _win32process = loaded
        if win32gui.IsIconic(int(hwnd)):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
        dpi_scale = 1.0
        try:
            import ctypes

            dpi_scale = max(
                1.0,
                float(ctypes.windll.user32.GetDpiForWindow(int(hwnd))) / 96.0,
            )
        except Exception:
            pass
        bounds = SurfaceBounds(
            int(left), int(top), int(right), int(bottom), dpi_scale=dpi_scale,
        )
        return bounds if bounds.valid() else None
    except Exception:
        return None


def _window_client_origin(hwnd: object, fallback: tuple[int, int]) -> tuple[int, int]:
    """Return the physical-screen origin of the native client area."""

    if sys.platform != "win32":
        return fallback
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return fallback
        win32gui, _win32process = loaded
        x, y = win32gui.ClientToScreen(int(hwnd), (0, 0))
        return int(x), int(y)
    except Exception:
        return fallback


def _foreground_native_window_id(snapshot: object) -> object:
    """Extract the HWND from the canonical foreground-window snapshot."""

    if isinstance(snapshot, (tuple, list)):
        return snapshot[0] if snapshot else 0
    return snapshot or 0


def _window_is_minimized(hwnd: object) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        return bool(loaded and loaded[0].IsIconic(int(hwnd)))
    except Exception:
        return False


def _window_capture_protection_reason(hwnd: object) -> str:
    """Return the declared Win32 capture restriction without changing it."""

    if sys.platform != "win32":
        return ""
    try:
        import ctypes

        affinity = ctypes.c_uint(0)
        get_affinity = ctypes.windll.user32.GetWindowDisplayAffinity
        get_affinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        get_affinity.restype = ctypes.c_bool
        queried = get_affinity(
            int(hwnd), ctypes.byref(affinity),
        )
        if not queried or affinity.value == 0:
            return ""
        if affinity.value == 0x11:  # WDA_EXCLUDEFROMCAPTURE
            return "window_excluded_from_capture"
        if affinity.value == 0x01:  # WDA_MONITOR
            return "window_monitor_only_capture"
        return "window_capture_affinity_restricted"
    except Exception:
        return ""


def _window_packaged_application_id(hwnd: object) -> str:
    """Return one window-bound stable AUMID, never a localized display name."""

    if sys.platform != "win32":
        return ""
    try:
        from osia.tools import desktop_control as dc
        from win32com.propsys import propsys, pscon

        value = propsys.SHGetPropertyStoreForWindow(int(hwnd)).GetValue(
            pscon.PKEY_AppUserModel_ID
        )
        app_id = str(value.GetValue() if value is not None else "").strip()
        return app_id if dc._safe_apps_folder_id(app_id) else ""
    except Exception:
        return ""


def _window_metadata(hwnd: object) -> tuple[str, int, str, SurfaceBounds | None]:
    if sys.platform != "win32":
        return "", 0, "", None
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return "", 0, "", None
        win32gui, win32process = loaded
        title = str(win32gui.GetWindowText(int(hwnd)) or "")
        class_name = str(win32gui.GetClassName(int(hwnd)) or "")
        _thread, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        return title, int(pid or 0), class_name, _window_bounds(hwnd)
    except Exception:
        return "", 0, "", None


def _owner_window_id(hwnd: object) -> int:
    if sys.platform != "win32":
        return 0
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        return int(loaded[0].GetWindow(int(hwnd), 4) or 0) if loaded else 0
    except Exception:
        return 0


def _window_client_bounds(hwnd: object) -> SurfaceBounds | None:
    if sys.platform != "win32":
        return None
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return None
        win32gui, _win32process = loaded
        left, top, right, bottom = win32gui.GetClientRect(int(hwnd))
        screen_left, screen_top = win32gui.ClientToScreen(
            int(hwnd), (int(left), int(top)),
        )
        screen_right, screen_bottom = win32gui.ClientToScreen(
            int(hwnd), (int(right), int(bottom)),
        )
        window = _window_bounds(hwnd)
        dpi_scale = window.dpi_scale if window is not None else 1.0
        bounds = SurfaceBounds(
            int(screen_left), int(screen_top),
            int(screen_right), int(screen_bottom),
            dpi_scale=dpi_scale,
        )
        return bounds if bounds.valid() else None
    except Exception:
        return None


def _window_is_cloaked(hwnd: object) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        value = ctypes.c_int(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            int(hwnd), 14, ctypes.byref(value), ctypes.sizeof(value),
        )
        return bool(result == 0 and value.value)
    except Exception:
        return False


def _native_window_surface_candidates(
    scene: DesktopScene,
) -> tuple[NativeWindowSurfaceCandidate, ...]:
    """Enumerate one exact root tree; never widen capture to the desktop."""

    native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    if native is None:
        return ()
    if sys.platform != "win32":
        try:
            native_hwnd = int(native.native_window_id or 0)
        except (TypeError, ValueError, OverflowError):
            native_hwnd = 0
        return (NativeWindowSurfaceCandidate(
            hwnd=native_hwnd,
            pid=int(native.pid),
            process_started_at=float(native.process_started_at),
            class_name=str(native.class_name or ""),
            title=scene.window_ref.accessible_label,
            parent_hwnd=0,
            owner_hwnd=0,
            root_hwnd=native_hwnd,
            is_window=True,
            visible=not scene.minimized,
            cloaked=False,
            bounds=scene.window_ref.bounds,
            client_bounds=scene.window_ref.bounds,
            surface_id=scene.window_ref.ref_id,
            generation=scene.window_ref.window_generation,
            uia_control_type=scene.window_ref.role,
            foreground=scene.foreground,
        ),)
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return ()
        win32gui, win32process = loaded
        semantic_hwnd = int(native.native_window_id)
        try:
            root_hwnd = int(
                win32gui.GetAncestor(semantic_hwnd, 2) or semantic_hwnd
            )
        except Exception:
            root_hwnd = semantic_hwnd
        root_hwnds = [root_hwnd]
        for candidate in dc._enum_app_windows_win32()[:80]:
            try:
                candidate_hwnd, _title, candidate_pid, _class = candidate
                if int(candidate_pid or 0) == int(native.pid):
                    root_hwnds.append(int(candidate_hwnd))
            except (TypeError, ValueError, OverflowError):
                continue
        hwnds = []
        for candidate_root in tuple(dict.fromkeys(root_hwnds)):
            if len(hwnds) >= 80:
                break
            hwnds.append(candidate_root)

            def add_child(hwnd, _extra):
                if len(hwnds) < 80:
                    hwnds.append(int(hwnd))
                return len(hwnds) < 80

            win32gui.EnumChildWindows(candidate_root, add_child, None)
        foreground_snapshot = dc._foreground_window_win32()
        foreground_hwnd = int(
            _foreground_native_window_id(foreground_snapshot) or 0
        )
        auto = dc._load_uiautomation()
    except Exception:
        return ()

    candidates: list[NativeWindowSurfaceCandidate] = []
    for hwnd in tuple(dict.fromkeys(hwnds))[:80]:
        try:
            is_window = bool(win32gui.IsWindow(hwnd))
            if not is_window:
                continue
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            title = str(win32gui.GetWindowText(hwnd) or "")
            class_name = str(win32gui.GetClassName(hwnd) or "")
            parent_hwnd = int(win32gui.GetParent(hwnd) or 0)
            owner_hwnd = int(win32gui.GetWindow(hwnd, 4) or 0)
            try:
                actual_root = int(win32gui.GetAncestor(hwnd, 2) or hwnd)
            except Exception:
                actual_root = root_hwnd
            if (
                int(pid or 0) != int(native.pid)
                and actual_root != root_hwnd
            ):
                # Admit a separate renderer process only when Windows proves
                # the HWND belongs to this exact semantic root tree.
                continue
            _candidate_application, candidate_started_at = _process_details(
                int(pid or 0)
            )
            if float(candidate_started_at or 0.0) <= 0.0:
                continue
            try:
                style = int(win32gui.GetWindowLong(hwnd, -16) or 0)
                exstyle = int(win32gui.GetWindowLong(hwnd, -20) or 0)
            except Exception:
                style = exstyle = 0
            uia_control_type = ""
            if auto is not None:
                try:
                    control = auto.ControlFromHandle(hwnd)
                    uia_control_type = str(
                        getattr(control, "ControlTypeName", "") or ""
                    )
                except Exception:
                    uia_control_type = ""
            dpi = 96
            monitor = ""
            try:
                import ctypes

                dpi = max(1, int(
                    ctypes.windll.user32.GetDpiForWindow(hwnd) or 96
                ))
                monitor = str(
                    int(ctypes.windll.user32.MonitorFromWindow(hwnd, 2) or 0)
                )
            except Exception:
                pass
            bounds = _window_bounds(hwnd)
            client_bounds = _window_client_bounds(hwnd)
            candidates.append(NativeWindowSurfaceCandidate(
                hwnd=hwnd,
                pid=int(pid),
                process_started_at=float(candidate_started_at),
                class_name=class_name,
                title=title,
                parent_hwnd=parent_hwnd,
                owner_hwnd=owner_hwnd,
                root_hwnd=actual_root,
                is_window=is_window,
                visible=bool(win32gui.IsWindowVisible(hwnd)),
                cloaked=_window_is_cloaked(hwnd),
                bounds=bounds,
                client_bounds=client_bounds,
                monitor=monitor,
                dpi=dpi,
                surface_id=(
                    scene.window_ref.ref_id
                    if hwnd == semantic_hwnd else
                    "nws_" + hashlib.sha256(
                        (
                            f"{pid}:{candidate_started_at:.6f}:"
                            f"{hwnd}:{class_name}"
                        ).encode("utf-8", errors="replace")
                    ).hexdigest()[:24]
                ),
                generation=(
                    scene.window_ref.window_generation
                    if hwnd == semantic_hwnd else
                    "win_" + hashlib.sha256(
                        (
                            f"{pid}:{candidate_started_at:.6f}:"
                            f"{hwnd}:{class_name}"
                        ).encode("utf-8", errors="replace")
                    ).hexdigest()[:24]
                ),
                uia_control_type=uia_control_type,
                content_host_signal=(
                    uia_control_type in {
                        "CustomControl", "DocumentControl", "PaneControl",
                    }
                ),
                tool_window=bool(exstyle & 0x00000080),
                popup_window=bool(owner_hwnd and style & 0x80000000),
                foreground=bool(hwnd == foreground_hwnd),
            ))
        except Exception:
            continue
    if not candidates:
        return ()
    content_roots = set()
    for root in tuple(
        item for item in candidates if item.hwnd == item.root_hwnd
    ):
        if any(
            item.hwnd != root.hwnd
            and item.root_hwnd == root.hwnd
            and item.content_host_signal
            and _surface_candidate_coverage(item, root.bounds)
            is SurfaceCoverage.CLIENT_CONTENT
            for item in candidates
        ):
            content_roots.add(root.hwnd)
    if content_roots:
        candidates = [
            replace(item, content_host_signal=True)
            if item.hwnd in content_roots else item
            for item in candidates
        ]
    return tuple(candidates)


def _visual_surface_selection_for_scene(
    scene: DesktopScene,
) -> tuple[
    tuple[NativeWindowSurfaceCandidate, ...],
    VisualSurfaceSelection,
]:
    candidates = _native_window_surface_candidates(scene)
    native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    try:
        semantic_hwnd = (
            int(native.native_window_id or 0) if native is not None else 0
        )
    except (TypeError, ValueError, OverflowError):
        semantic_hwnd = 0
    return candidates, select_application_visual_surface(
        candidates, semantic_hwnd,
    )


def _control_bounds(control, window_bounds: SurfaceBounds) -> SurfaceBounds | None:
    try:
        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        bounds = SurfaceBounds(
            left,
            top,
            right,
            bottom,
            left - window_bounds.left,
            top - window_bounds.top,
            right - window_bounds.left,
            bottom - window_bounds.top,
            dpi_scale=window_bounds.dpi_scale,
        )
        return bounds if bounds.valid() else None
    except Exception:
        return None


def _bool_property(control, name: str, default: bool = False) -> bool:
    try:
        value = getattr(control, name)
        return bool(value() if callable(value) else value)
    except Exception:
        return bool(default)


def _selection_state(control) -> bool:
    if _bool_property(control, "IsSelected", False):
        return True
    try:
        pattern = control.GetSelectionItemPattern()
        return bool(pattern and pattern.IsSelected)
    except Exception:
        return False


def _supported_patterns(control) -> tuple[str, ...]:
    """Expose only pattern names, never Value/Text/password contents."""

    supported = []
    action_methods = {
        pattern_name: action_method
        for pattern_name, _getter_name, action_method
        in _UIA_ACTION_PATTERN_METHODS
    }
    for safe_name, method_name in (
        ("Selection", "GetSelectionPattern"),
        ("SelectionItem", "GetSelectionItemPattern"),
        ("Invoke", "GetInvokePattern"),
        ("Value", "GetValuePattern"),
        ("Text", "GetTextPattern"),
        ("ExpandCollapse", "GetExpandCollapsePattern"),
        ("Toggle", "GetTogglePattern"),
        ("Window", "GetWindowPattern"),
    ):
        try:
            getter = getattr(control, method_name, None)
            pattern = getter() if callable(getter) else None
            if pattern is None:
                continue
            action_method = action_methods.get(safe_name)
            if action_method and not callable(getattr(pattern, action_method, None)):
                continue
            supported.append(safe_name)
        except Exception:
            continue
    return tuple(supported)


def _fresh_uia_action_methods(control) -> dict[str, object]:
    """Return only canonical UIA actions callable on this fresh reference."""

    available = {}
    for pattern_name, getter_name, method_name in _UIA_ACTION_PATTERN_METHODS:
        try:
            getter = getattr(control, getter_name, None)
            pattern = getter() if callable(getter) else None
            method = getattr(pattern, method_name, None) if pattern else None
        except Exception:
            continue
        if callable(method):
            available[pattern_name] = method
    return available


def _dispatch_fresh_uia_action_bounded(
    auto,
    *,
    native_window_id: int,
    runtime_id: tuple[int, ...],
    action_pattern: str,
    observed_patterns: Iterable[str],
    invoke_preflight_reason: str = "",
    journal_dispatch_started=None,
    journal_dispatch_returned=None,
) -> dict:
    """Bound one UIA provider call while preserving its exact dispatch state."""

    done = threading.Event()
    cancel_before_dispatch = threading.Event()
    dispatch_gate = threading.Lock()
    state = {
        "dispatch_started": False,
        "dispatched_at": 0.0,
        "completed": False,
        "reason_code": "",
        "worker_may_be_active": False,
        "worker_native_id": 0,
        "automation_id": "",
        "fresh_supported_patterns": [],
        "selected_pattern": action_pattern,
        "selected_pattern_available": False,
    }

    def _worker() -> None:
        cancellation_enabled = False
        try:
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(int(native_window_id))
                control = _find_runtime_control(root, runtime_id)
                if control is None:
                    state["reason_code"] = "stale_semantic_reference"
                    return
                try:
                    state["automation_id"] = _safe_label(control.AutomationId)
                except Exception:
                    pass
                fresh_actions = _fresh_uia_action_methods(control)
                state["fresh_supported_patterns"] = list(fresh_actions)
                allowed_patterns = set(observed_patterns)
                selected_pattern = action_pattern or next((
                    pattern_name
                    for pattern_name, _getter_name, _method_name
                    in _UIA_ACTION_PATTERN_METHODS
                    if pattern_name in fresh_actions
                    and pattern_name in allowed_patterns
                ), "")
                state["selected_pattern"] = selected_pattern
                method = (
                    fresh_actions.get(selected_pattern)
                    if selected_pattern in allowed_patterns else
                    None
                )
                state["selected_pattern_available"] = callable(method)
                if not callable(method):
                    state["reason_code"] = "uia_action_pattern_unavailable"
                    return
                if selected_pattern == "Invoke" and invoke_preflight_reason:
                    state["reason_code"] = invoke_preflight_reason
                    return
                with dispatch_gate:
                    if cancel_before_dispatch.is_set():
                        state["reason_code"] = "uia_action_dispatch_timed_out_before_effect"
                        return
                    if (
                        callable(journal_dispatch_started)
                        and not journal_dispatch_started(
                            automation_id=state["automation_id"],
                            uia_pattern=selected_pattern,
                        )
                    ):
                        state["reason_code"] = "primitive_journal_unavailable"
                        return
                    state["dispatched_at"] = time.time()
                    state["dispatch_started"] = True
                    state["worker_native_id"] = threading.get_native_id()
                try:
                    import ctypes

                    cancellation_enabled = (
                        ctypes.windll.ole32.CoEnableCallCancellation(None) >= 0
                    )
                except Exception:
                    cancellation_enabled = False
                method()
                if (
                    callable(journal_dispatch_returned)
                    and not journal_dispatch_returned()
                ):
                    state["reason_code"] = (
                        "primitive_journal_return_unavailable"
                    )
                    return
                state["completed"] = True
        except Exception:
            state["reason_code"] = (
                "uia_activation_outcome_unknown"
                if state["dispatch_started"] else
                "native_accessibility_invocation_unavailable"
            )
        finally:
            if cancellation_enabled:
                try:
                    import ctypes

                    ctypes.windll.ole32.CoDisableCallCancellation(None)
                except Exception:
                    pass
            done.set()

    worker = threading.Thread(
        target=_worker,
        name="osia-uia-action",
        daemon=True,
    )
    worker.start()
    if not done.wait(_UIA_ACTION_DISPATCH_TIMEOUT_SECONDS):
        with dispatch_gate:
            if not state["dispatch_started"]:
                cancel_before_dispatch.set()
                state["reason_code"] = (
                    "uia_action_dispatch_timed_out_before_effect"
                )
            else:
                state["reason_code"] = "uia_action_dispatch_timed_out"
                try:
                    import ctypes

                    ctypes.windll.ole32.CoCancelCall(
                        int(state["worker_native_id"]),
                        0,
                    )
                except Exception:
                    pass
        done.wait(_UIA_ACTION_CANCEL_SETTLE_SECONDS)
        state["worker_may_be_active"] = not done.is_set()
    return dict(state)


def _durable_primitive_journal_callbacks(
    *,
    application_identity: str,
    surface_ref: str,
    native_window_id: object,
    window_generation: str,
    runtime_identity: str,
):
    """Bind the current durable attempt without creating another state store."""

    try:
        from osia.tool_attempts import (
            current_tool_attempt_id,
            current_tool_attempt_store,
        )

        attempt_id = current_tool_attempt_id()
        store = current_tool_attempt_store()
    except Exception:
        attempt_id = ""
        store = None
    if not attempt_id:
        return None, None
    if store is None:
        return (lambda **_kwargs: False), (lambda: False)
    try:
        durable = store.get_attempt(attempt_id) or {}
        journal = list(durable.get("primitive_journal") or [])
        prepared = next((
            item for item in journal
            if item.get("lifecycle") == "prepared"
            and (
                item.get("surface_ref") == str(surface_ref or "")
                or (
                    runtime_identity
                    and item.get("runtime_identity") == runtime_identity
                )
            )
        ), None)
        if prepared is None and len(journal) == 1:
            prepared = journal[0]
        primitive_id = str(
            prepared.get("primitive_id") if isinstance(prepared, dict) else ""
        )
    except Exception:
        primitive_id = ""

    def _started(*, automation_id: str = "", uia_pattern: str = "") -> bool:
        if not primitive_id:
            return False
        try:
            updated = store.record_primitive_dispatch_started(
                attempt_id,
                primitive_id=primitive_id,
                application_identity=application_identity,
                surface_ref=surface_ref,
                native_window_id=str(native_window_id or ""),
                window_generation=window_generation,
                runtime_identity=runtime_identity,
                automation_id=automation_id,
                uia_pattern=uia_pattern,
            )
            entry = next(
                (
                    item for item in updated.get("primitive_journal") or []
                    if item.get("primitive_id") == primitive_id
                ),
                {},
            )
            return entry.get("lifecycle") == "dispatch_started"
        except Exception:
            return False

    def _returned() -> bool:
        if not primitive_id:
            return False
        try:
            updated = store.record_primitive_dispatch_returned(
                attempt_id,
                primitive_id=primitive_id,
            )
            entry = next(
                (
                    item for item in updated.get("primitive_journal") or []
                    if item.get("primitive_id") == primitive_id
                ),
                {},
            )
            return entry.get("lifecycle") == "dispatch_returned"
        except Exception:
            return False

    return _started, _returned


def _surface_kind(role: str, *, modal: bool = False) -> SurfaceKind:
    if modal or role == "WindowControl":
        return SurfaceKind.DIALOG if modal else SurfaceKind.WINDOW
    return {
        "TabControl": SurfaceKind.TAB,
        "TabItemControl": SurfaceKind.TAB,
        "PaneControl": SurfaceKind.PANE,
        "DocumentControl": SurfaceKind.PANE,
        "ListControl": SurfaceKind.LIST,
        "ListItemControl": SurfaceKind.CONVERSATION,
        "TreeControl": SurfaceKind.LIST,
        "TreeItemControl": SurfaceKind.CONVERSATION,
        "ToolBarControl": SurfaceKind.TOOLBAR,
        "GroupControl": SurfaceKind.PANE,
    }.get(role, SurfaceKind.CONTROL)


def _scene_signature(rows: Iterable[tuple]) -> str:
    material = repr(tuple(rows))
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]


def _observe_window_scene(
    window: tuple[object, str, int],
    *,
    target: str = "",
    foreground: bool,
    class_name_hint: str = "",
) -> DesktopScene | None:
    """Observe one exact top-level window without exposing its native handle."""

    if sys.platform != "win32":
        return None
    _ensure_thread_physical_pixels()
    from osia.tools import desktop_control as dc

    hwnd, title, pid = window
    if not hwnd or not pid:
        return None
    minimized = _window_is_minimized(hwnd)
    window_bounds = _window_bounds(hwnd)
    if window_bounds is None and not minimized:
        return None
    host_application_id, process_started_at = _process_details(pid)
    packaged_application_id = _window_packaged_application_id(hwnd)
    class_name = str(class_name_hint or "")
    if not class_name:
        try:
            loaded = dc._load_win32()
            class_name = loaded[0].GetClassName(hwnd) if loaded else ""
        except Exception:
            class_name = ""

    rows = []
    controls = []
    root_is_modal = False
    uia_tree_observed = False
    auto = dc._load_uiautomation()
    if auto is not None:
        try:
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(int(hwnd))
                uia_tree_observed = root is not None
                root_is_modal = _bool_property(root, "IsModal", False)
                try:
                    root_children = root.GetChildren() if root else ()
                except Exception:
                    root_children = ()
                root_runtime = _runtime_id(root) if root else ()
                root_ancestor = _runtime_signature(
                    root_runtime, "WindowControl", "window",
                ) or "window"
                frontier = [
                    (child, None, root_ancestor, 1)
                    for child in (root_children or ())
                ]
                while frontier and len(controls) < _MAX_UIA_NODES:
                    control, parent_index, ancestor, depth = frontier.pop(0)
                    try:
                        role = str(control.ControlTypeName or "")
                    except Exception:
                        role = ""
                    try:
                        password = bool(control.IsPassword)
                    except Exception:
                        password = False
                    try:
                        label = _safe_label(control.Name, password=password)
                    except Exception:
                        label = ""
                    try:
                        automation_id = _safe_label(control.AutomationId)
                    except Exception:
                        automation_id = ""
                    try:
                        control_process_id = int(control.ProcessId)
                    except Exception:
                        control_process_id = int(pid)
                    runtime_id = _runtime_id(control)
                    bounds = _control_bounds(control, window_bounds)
                    selected = _selection_state(control)
                    focused = _bool_property(control, "HasKeyboardFocus", False)
                    enabled = _bool_property(control, "IsEnabled", True)
                    modal = _bool_property(control, "IsModal", False)
                    patterns = _supported_patterns(control)
                    index = len(controls)
                    controls.append({
                        "control": control,
                        "parent_index": parent_index,
                        "ancestor": ancestor,
                        "role": role or "CustomControl",
                        "label": label or automation_id,
                        "automation_id": automation_id,
                        "process_id": control_process_id,
                        "runtime_id": runtime_id,
                        "bounds": bounds,
                        "selected": selected,
                        "focused": focused,
                        "enabled": enabled,
                        "modal": modal,
                        "patterns": patterns,
                    })
                    rows.append((
                        runtime_id,
                        role,
                        parent_index,
                        selected if _surface_kind(role, modal=modal) in {
                            SurfaceKind.TAB,
                            SurfaceKind.PANE,
                            SurfaceKind.CONVERSATION,
                        } else False,
                    ))
                    if depth < 12:
                        try:
                            children = control.GetChildren()
                        except Exception:
                            children = ()
                        next_ancestor = _runtime_signature(
                            runtime_id, role, ancestor,
                        ) or ancestor
                        for child in children or ():
                            frontier.append((child, index, next_ancestor, depth + 1))
        except Exception:
            controls = []
            rows = []
            root_is_modal = False
            uia_tree_observed = False

    application_id = _semantic_application_id(
        host_application_id,
        int(pid),
        controls,
    )
    content_application_id = (
        application_id
        if (
            _application_identity(host_application_id) == "applicationframehost"
            and _application_identity(application_id) != "applicationframehost"
        ) else ""
    )
    application_hint = str(target or "").strip() or _strategy_required_application()
    if (
        _application_identity(host_application_id) == "applicationframehost"
        and packaged_application_id
        and application_hint
    ):
        try:
            packaged_match = bool(
                dc._packaged_app_identity_score(
                    application_hint,
                    packaged_application_id,
                )
            )
        except Exception:
            packaged_match = False
        if packaged_match:
            application_id = _application_identity(application_hint)

    signature = _scene_signature(
        rows or [(
            class_name,
            (
                window_bounds.to_safe_dict()
                if window_bounds is not None else {"minimized": True}
            ),
            root_is_modal,
            packaged_application_id,
        )]
    )
    process_ref, generation, epoch = DESKTOP_SURFACE_REGISTRY.observe_window_scene(
        application_id=application_id,
        pid=int(pid),
        process_started_at=process_started_at,
        native_window_id=hwnd,
        class_name=class_name,
        scene_signature=signature,
    )
    native_window = NativeSurfaceIdentity(
        pid=int(pid),
        process_started_at=process_started_at,
        native_window_id=hwnd,
        class_name=class_name,
    )
    owner_ref_id = None
    owner_ref = None
    owner_hwnd = _owner_window_id(hwnd)
    if owner_hwnd:
        owner_title, owner_pid, owner_class, owner_bounds = _window_metadata(owner_hwnd)
        if owner_pid == int(pid) and owner_bounds is not None:
            owner_process_ref, owner_generation, owner_epoch = (
                DESKTOP_SURFACE_REGISTRY.observe_window_scene(
                    application_id=application_id,
                    pid=owner_pid,
                    process_started_at=process_started_at,
                    native_window_id=owner_hwnd,
                    class_name=owner_class,
                    scene_signature=_scene_signature((
                        (owner_class, owner_title, owner_bounds.to_safe_dict()),
                    )),
                )
            )
            owner_ref = DESKTOP_SURFACE_REGISTRY.register(
                application_id=application_id,
                process_ref=owner_process_ref,
                generation=owner_generation,
                surface_kind=SurfaceKind.WINDOW,
                role="WindowControl",
                accessible_label=_safe_label(owner_title),
                selected=False,
                focused=False,
                enabled=True,
                parent_ref=None,
                bounds=owner_bounds,
                runtime_signature="",
                structural_epoch=owner_epoch,
                scene_signature="owner_window",
                native=NativeSurfaceIdentity(
                    pid=owner_pid,
                    process_started_at=process_started_at,
                    native_window_id=owner_hwnd,
                    class_name=owner_class,
                ),
                stable_identity="window",
                patterns=("Window",),
            )
            owner_ref_id = owner_ref.ref_id
    window_ref = DESKTOP_SURFACE_REGISTRY.register(
        application_id=application_id,
        process_ref=process_ref,
        generation=generation,
        surface_kind=(
            SurfaceKind.DIALOG
            if owner_ref_id or root_is_modal
            else SurfaceKind.WINDOW
        ),
        role="WindowControl",
        accessible_label=_safe_label(title),
        selected=bool(foreground),
        focused=bool(foreground),
        enabled=True,
        parent_ref=owner_ref_id,
        bounds=window_bounds,
        runtime_signature="",
        structural_epoch=epoch,
        scene_signature=signature,
        native=native_window,
        stable_identity="window",
        patterns=("Window",),
    )
    refs: list[DesktopSurfaceRef] = [
        window_ref,
        *([owner_ref] if owner_ref is not None else []),
    ]
    ref_by_index: dict[int, str] = {}
    for index, item in enumerate(controls):
        runtime_id = item["runtime_id"]
        role = item["role"]
        parent_ref = ref_by_index.get(item["parent_index"], window_ref.ref_id)
        runtime_signature = _runtime_signature(
            runtime_id, role, item["ancestor"], item["automation_id"],
        )
        bounds = item["bounds"]
        stable_identity = runtime_signature or hashlib.sha256(
            repr((role, item["automation_id"], item["label"], bounds)).encode(
                "utf-8", errors="replace",
            )
        ).hexdigest()[:24]
        native = NativeSurfaceIdentity(
            pid=int(pid),
            process_started_at=process_started_at,
            native_window_id=hwnd,
            runtime_id=runtime_id,
            ancestor_signature=item["ancestor"],
            class_name=class_name,
        )
        ref = DESKTOP_SURFACE_REGISTRY.register(
            application_id=application_id,
            process_ref=process_ref,
            generation=generation,
            surface_kind=_surface_kind(role, modal=item["modal"]),
            role=role,
            accessible_label=item["label"],
            selected=item["selected"],
            focused=bool(foreground and item["focused"]),
            enabled=item["enabled"],
            parent_ref=parent_ref,
            bounds=bounds,
            runtime_signature=runtime_signature,
            structural_epoch=epoch,
            scene_signature=signature,
            native=native,
            stable_identity=stable_identity,
            patterns=item["patterns"],
            automation_id=item["automation_id"],
        )
        refs.append(ref)
        ref_by_index[index] = ref.ref_id
    scene = DesktopScene(
        application_id=application_id,
        window_ref=window_ref,
        surfaces=tuple(refs),
        structural_epoch=epoch,
        scene_signature=signature,
        accessibility_complete=sum(
            bool(item["label"] or item["patterns"])
            and item["role"] in {
                "ButtonControl", "CheckBoxControl", "ComboBoxControl",
                "EditControl", "HyperlinkControl", "ListControl",
                "ListItemControl", "MenuControl", "MenuItemControl",
                "RadioButtonControl", "TabControl", "TabItemControl",
                "TreeControl", "TreeItemControl",
            }
            for item in controls
        ) >= 2,
        observed_at=time.time(),
        uia_tree_observed=uia_tree_observed,
        foreground=bool(foreground),
        host_application_id=host_application_id,
        content_application_id=content_application_id,
        packaged_application_id=packaged_application_id,
        minimized=bool(minimized),
    )
    return scene


def observe_foreground_scene(*, target: str = "") -> DesktopScene | None:
    """Observe one exact foreground window without exposing its native handle."""

    if sys.platform != "win32":
        return None
    from osia.tools import desktop_control as dc

    foreground = dc._foreground_window_win32()
    if not foreground:
        return None
    scene = _observe_window_scene(
        foreground,
        target=target,
        foreground=True,
    )
    if scene is not None:
        _LAST_SCENE.set(scene)
    return scene


@dataclass(frozen=True)
class TargetSurfaceSelection:
    requested_application: str
    status: str
    foreground_scene: DesktopScene | None
    target_scene: DesktopScene | None
    identity_strength: int = 0
    identity_evidence: str = ""
    candidate_count: int = 0
    top_level_surfaces: tuple[dict, ...] = ()
    requested_candidates: tuple[dict, ...] = ()
    resolved_candidates: tuple[dict, ...] = ()
    target_conflict: bool = False


@dataclass(frozen=True)
class _LaunchTargetCorrelation:
    application_id: str
    process_ref: str
    window_generation: str
    correlated_at: float


_LAUNCH_TARGET_TTL_SECONDS = 90.0
_LAUNCH_TARGETS: dict[str, _LaunchTargetCorrelation] = {}
_LAUNCH_TARGETS_LOCK = threading.Lock()


def _remember_launch_target(scene: DesktopScene) -> None:
    application = _application_identity(scene.application_id)
    if not application:
        return
    correlation = _LaunchTargetCorrelation(
        application_id=application,
        process_ref=scene.window_ref.process_ref,
        window_generation=scene.window_ref.window_generation,
        correlated_at=time.time(),
    )
    with _LAUNCH_TARGETS_LOCK:
        _LAUNCH_TARGETS[application] = correlation


def _current_launch_target(application: str) -> _LaunchTargetCorrelation | None:
    identity = _application_identity(application)
    if not identity:
        return None
    now = time.time()
    with _LAUNCH_TARGETS_LOCK:
        correlation = _LAUNCH_TARGETS.get(identity)
        if (
            correlation is not None
            and now - correlation.correlated_at > _LAUNCH_TARGET_TTL_SECONDS
        ):
            _LAUNCH_TARGETS.pop(identity, None)
            return None
        return correlation


def _collapse_exact_scene_duplicates(
    scenes: Iterable[DesktopScene],
) -> tuple[DesktopScene, ...]:
    """Collapse exact native records and one proven UWP host/content pair."""

    grouped: dict[tuple[str, str], list[DesktopScene]] = {}
    for scene in scenes:
        resolution = DESKTOP_SURFACE_REGISTRY.resolve(scene.window_ref.ref_id)
        if resolution.status == "stale":
            continue
        key = (
            str(scene.window_ref.process_ref or ""),
            str(scene.window_ref.window_generation or ""),
        )
        if not all(key):
            key = ("scene", scene.window_ref.ref_id)
        grouped.setdefault(key, []).append(scene)
    collapsed = []
    for records in grouped.values():
        collapsed.append(max(
            records,
            key=lambda scene: (
                bool(scene.content_application_id),
                bool(scene.uia_tree_observed),
                len(scene.surfaces),
                float(scene.observed_at),
            ),
        ))
    logical = []
    for scene in collapsed:
        duplicate_index = next((
            index for index, existing in enumerate(logical)
            if _same_packaged_host_content_window(existing, scene)
        ), None)
        if duplicate_index is None:
            logical.append(scene)
            continue
        logical[duplicate_index] = max(
            (logical[duplicate_index], scene),
            key=lambda candidate: (
                bool(candidate.content_application_id),
                bool(candidate.packaged_application_id),
                bool(candidate.uia_tree_observed),
                len(candidate.surfaces),
                float(candidate.observed_at),
            ),
        )
    return tuple(logical)


def _same_packaged_host_content_window(
    first: DesktopScene,
    second: DesktopScene,
) -> bool:
    """Correlate one UWP frame and its inset content HWND, never peers."""

    for host, content in ((first, second), (second, first)):
        if (
            _application_identity(
                host.host_application_id or host.application_id
            ) != "applicationframehost"
            or not host.packaged_application_id
            or not host.content_application_id
            or _application_identity(host.content_application_id)
            != _application_identity(content.application_id)
            or _application_identity(
                content.host_application_id or content.application_id
            ) == "applicationframehost"
            or _normalized_surface_text(
                host.window_ref.accessible_label
            ) != _normalized_surface_text(
                content.window_ref.accessible_label
            )
        ):
            continue
        outer = host.window_ref.bounds
        inner = content.window_ref.bounds
        if outer is None or inner is None:
            continue
        insets = (
            inner.left - outer.left,
            inner.top - outer.top,
            outer.right - inner.right,
            outer.bottom - inner.bottom,
        )
        if all(0 <= value <= 32 for value in insets):
            return True
    return False


def _strategy_required_application() -> str:
    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None:
        return ""
    node = (
        strategy.node_by_id(strategy._frozen_node_id)
        if strategy.frozen_reason else None
    ) or strategy.goal
    return str(node.required_application or "").strip()


def _strategy_target_hint() -> dict:
    """Return the current bounded hint without upgrading it to exact truth."""

    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None:
        return {}
    node = (
        strategy.node_by_id(strategy._frozen_node_id)
        if strategy.frozen_reason else None
    ) or strategy.goal
    hint = node.target_hint if isinstance(node.target_hint, dict) else {}
    if hint:
        return dict(hint)
    application = str(node.required_application or "").strip()
    return (
        {
            "raw_text": application,
            "normalized_value": application,
            "source": "legacy_semantic_mission",
            "confidence": 0.5,
            "ambiguity": True,
            "exactness": False,
            "span": [0, 0],
            "candidates": [application],
        }
        if application else {}
    )


def _requested_application(target: str) -> str:
    value = " ".join(str(target or "").split())
    if not value:
        return _strategy_required_application()
    if re.fullmatch(r"[\d\s\u00a0\u202f.,'*/+\-=×÷−]+", value):
        return ""
    normalized = " ".join(_application_words(value))
    generic_words = {
        "app", "application", "current", "desktop", "exact", "exacte",
        "exactement", "foreground", "fenetre", "native", "premier",
        "plan", "requested", "visually", "visuellement", "window",
    }
    tokens = [
        token for token in re.findall(r"[\w.-]+", normalized)
        if token not in generic_words
    ]
    if 1 <= len(tokens) <= 4:
        return " ".join(tokens)[:160]
    if value:
        return value[:160]
    return _strategy_required_application()


def _target_candidates(target: str) -> tuple[dict, ...]:
    """Return explicit and strategy candidates in descending authority."""

    raw_target = " ".join(str(target or "").split())
    has_explicit_target = bool(
        raw_target
        and not re.fullmatch(
            r"[\d\s\u00a0\u202f.,'*/+\-=×÷−]+", raw_target,
        )
    )
    explicit = _requested_application(target) if has_explicit_target else ""
    hint = _strategy_target_hint()
    candidates = []
    if has_explicit_target and explicit:
        candidates.append({
            "value": explicit,
            "source": "model_tool_argument",
            "confidence": 1.0,
            "exactness": True,
            "ambiguity": False,
        })
    hinted = str(hint.get("normalized_value") or "").strip()
    if hinted and _application_identity(hinted) != _application_identity(explicit):
        try:
            confidence = min(
                1.0, max(0.0, float(hint.get("confidence") or 0.0)),
            )
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        candidates.append({
            "value": hinted,
            "source": str(hint.get("source") or "strategy_target_hint")[:80],
            "confidence": confidence,
            "exactness": hint.get("exactness") is True,
            "ambiguity": hint.get("ambiguity") is not False,
        })
    return tuple(candidates)


def _window_title_matches_application(title: str, expected: str) -> bool:
    target = _application_identity(expected)
    if not target:
        return False
    if _application_identity(title) == target:
        return True
    expected_words = _application_words(expected)
    title_words = _application_words(title)
    if not expected_words or len(expected_words) > len(title_words):
        return False
    width = len(expected_words)
    return any(
        title_words[index:index + width] == expected_words
        for index in range(len(title_words) - width + 1)
    )


def _scene_matches_application(
    scene: DesktopScene,
    expected: str,
) -> bool:
    """Match one semantic app against proven process/title/packaged identity."""

    if not str(expected or "").strip():
        return True
    rank, _evidence = _scene_application_identity(scene, expected)
    return rank > 0


def _surface_matches_application(
    surface: DesktopSurfaceRef,
    expected: str,
) -> bool:
    if not str(expected or "").strip():
        return True
    return bool(
        _application_identity(surface.application_id)
        == _application_identity(expected)
        or _window_title_matches_application(
            surface.accessible_label, expected,
        )
    )


def _safe_top_level_surface(
    *,
    application: str,
    title: str,
    class_name: str,
    foreground: bool,
) -> dict:
    return {
        "application": str(application or "")[:160],
        "title": _safe_label(title),
        "window_class": _safe_label(class_name),
        "foreground": bool(foreground),
    }


def _resolve_target_scene(
    scenes: Iterable[DesktopScene],
    requested_application: str,
    *,
    foreground_scene: DesktopScene | None,
    top_level_surfaces: Iterable[dict] = (),
) -> TargetSurfaceSelection:
    ranked = []
    for scene in _collapse_exact_scene_duplicates(scenes):
        rank, evidence = _scene_application_identity(
            scene, requested_application,
        )
        if rank > 0:
            ranked.append((rank, evidence, scene))
    if not ranked:
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="target_missing",
            foreground_scene=foreground_scene,
            target_scene=None,
            top_level_surfaces=tuple(top_level_surfaces),
        )
    correlation = _current_launch_target(requested_application)
    if correlation is not None:
        correlated = [
            item for item in ranked
            if (
                item[2].window_ref.process_ref == correlation.process_ref
                and item[2].window_ref.window_generation
                == correlation.window_generation
            )
        ]
        if len(correlated) == 1:
            rank, evidence, scene = correlated[0]
            return TargetSurfaceSelection(
                requested_application=requested_application,
                status="exact_target",
                foreground_scene=foreground_scene,
                target_scene=scene,
                identity_strength=rank,
                identity_evidence="current_open_attempt:" + evidence,
                candidate_count=1,
                top_level_surfaces=tuple(top_level_surfaces),
            )
    best_rank = max(item[0] for item in ranked)
    best = [item for item in ranked if item[0] == best_rank]
    if len(best) > 1:
        primary_application_windows = [
            item for item in best
            if item[2].window_ref.surface_kind is SurfaceKind.WINDOW
            and not item[2].window_ref.parent_ref
        ]
        if (
            len(primary_application_windows) == 1
            and all(
                item in primary_application_windows
                or item[2].window_ref.surface_kind is SurfaceKind.DIALOG
                or bool(item[2].window_ref.parent_ref)
                for item in best
            )
        ):
            best = primary_application_windows
    if len(best) != 1:
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="target_ambiguous",
            foreground_scene=foreground_scene,
            target_scene=None,
            identity_strength=best_rank,
            candidate_count=len(best),
            top_level_surfaces=tuple(top_level_surfaces),
        )
    rank, evidence, scene = best[0]
    return TargetSurfaceSelection(
        requested_application=requested_application,
        status="exact_target",
        foreground_scene=foreground_scene,
        target_scene=scene,
        identity_strength=rank,
        identity_evidence=evidence,
        candidate_count=1,
        top_level_surfaces=tuple(top_level_surfaces),
    )


def _select_desktop_target(target: str = "") -> TargetSurfaceSelection:
    foreground_scene = observe_foreground_scene(target=target)
    requested_candidates = _target_candidates(target)
    requested_application = str(
        (requested_candidates[0] if requested_candidates else {}).get("value")
        or ""
    )
    if foreground_scene is None:
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="desktop_surface_unavailable",
            foreground_scene=None,
            target_scene=None,
            requested_candidates=requested_candidates,
        )
    foreground_summary = _safe_top_level_surface(
        application=foreground_scene.application_id,
        title=foreground_scene.window_ref.accessible_label,
        class_name=(
            DESKTOP_SURFACE_REGISTRY.native_identity(
                foreground_scene.window_ref.ref_id,
            ).class_name
            if DESKTOP_SURFACE_REGISTRY.native_identity(
                foreground_scene.window_ref.ref_id,
            ) is not None else ""
        ),
        foreground=True,
    )
    if not requested_application:
        return TargetSurfaceSelection(
            requested_application="",
            status="current_foreground",
            foreground_scene=foreground_scene,
            target_scene=foreground_scene,
            identity_strength=100,
            identity_evidence="current_foreground_without_application_target",
            candidate_count=1,
            top_level_surfaces=(foreground_summary,),
            requested_candidates=requested_candidates,
        )
    foreground_matches = [
        (*_scene_application_identity(foreground_scene, item["value"]), item)
        for item in requested_candidates
    ]
    foreground_matches = [item for item in foreground_matches if item[0] > 0]
    foreground_matches.sort(
        key=lambda item: (
            item[2].get("source") == "model_tool_argument",
            item[2].get("exactness") is True,
            float(item[2].get("confidence") or 0.0),
            item[0],
        ),
        reverse=True,
    )
    foreground_rank, foreground_evidence = (
        foreground_matches[0][0], foreground_matches[0][1]
    ) if foreground_matches else (0, "")
    foreground_is_packaged_frame = bool(
        _application_identity(
            foreground_scene.host_application_id
            or foreground_scene.application_id
        ) == "applicationframehost"
        or foreground_scene.packaged_application_id
    )
    foreground_authority = (
        foreground_matches[0][2] if foreground_matches else {}
    )
    matched_candidate_values = {
        _application_identity(item[2].get("value"))
        for item in foreground_matches
    }
    unresolved_strong_candidates = [
        item for item in requested_candidates
        if _application_identity(item.get("value"))
        not in matched_candidate_values
        and (
            item.get("exactness") is True
            or float(item.get("confidence") or 0.0) >= 0.9
        )
    ]
    if (
        foreground_rank > 0
        and (
            len(requested_candidates) == 1
            or (
                foreground_authority.get("source") == "model_tool_argument"
                and not unresolved_strong_candidates
            )
        )
        and (
        sys.platform != "win32" or not foreground_is_packaged_frame
        )
    ):
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="exact_target",
            foreground_scene=foreground_scene,
            target_scene=foreground_scene,
            identity_strength=foreground_rank,
            identity_evidence=foreground_evidence,
            candidate_count=1,
            top_level_surfaces=(foreground_summary,),
            requested_candidates=requested_candidates,
            resolved_candidates=tuple(
                dict(item[2]) for item in foreground_matches
            ),
        )
    if sys.platform != "win32":
        selection = _resolve_target_scene(
            (), requested_application,
            foreground_scene=foreground_scene,
            top_level_surfaces=(foreground_summary,),
        )
        return TargetSurfaceSelection(
            **{
                **selection.__dict__,
                "requested_candidates": requested_candidates,
            },
        )

    from osia.tools import desktop_control as dc

    foreground_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        foreground_scene.window_ref.ref_id,
    )
    foreground_hwnd = (
        foreground_native.native_window_id if foreground_native is not None else None
    )
    candidates = [foreground_scene]
    summaries = [foreground_summary]
    for hwnd, title, pid, class_name in dc._enum_app_windows_win32()[:80]:
        if foreground_hwnd is not None and int(hwnd) == int(foreground_hwnd):
            continue
        host_application, _started_at = _process_details(pid)
        summaries.append(_safe_top_level_surface(
            application=host_application,
            title=title,
            class_name=class_name,
            foreground=False,
        ))
        host_identity = _application_identity(host_application)
        if not any(
            host_identity == _application_identity(item["value"])
            or host_identity == "applicationframehost"
            or _window_title_matches_application(title, item["value"])
            for item in requested_candidates
        ):
            continue
        observed = _observe_window_scene(
            (hwnd, title, pid),
            target=target,
            foreground=False,
            class_name_hint=class_name,
        )
        if observed is not None:
            candidates.append(observed)
    resolved = []
    unresolved = []
    for candidate in requested_candidates:
        candidate_selection = _resolve_target_scene(
            candidates,
            str(candidate["value"]),
            foreground_scene=foreground_scene,
            top_level_surfaces=summaries[:80],
        )
        if candidate_selection.target_scene is not None:
            resolved.append((candidate_selection, candidate))
        else:
            unresolved.append((candidate_selection, candidate))
    target_scenes = {
        item[0].target_scene.window_ref.ref_id
        for item in resolved if item[0].target_scene is not None
    }
    if len(target_scenes) > 1:
        explicit = [
            item for item in resolved
            if item[1].get("source") == "model_tool_argument"
        ]
        conflicting_strong = [
            item for item in resolved
            if item not in explicit
            and (
                item[1].get("exactness") is True
                or float(item[1].get("confidence") or 0.0) >= 0.9
            )
        ]
        if len(explicit) == 1 and not conflicting_strong:
            selected, selected_authority = explicit[0]
            selected_ref = selected.target_scene.window_ref.ref_id
            aliases = tuple(
                dict(candidate)
                for candidate_selection, candidate in resolved
                if candidate_selection.target_scene is not None
                and candidate_selection.target_scene.window_ref.ref_id
                == selected_ref
            )
            selection = TargetSurfaceSelection(
                **{
                    **selected.__dict__,
                    "requested_application": str(
                        selected_authority["value"]
                    ),
                    "requested_candidates": requested_candidates,
                    "resolved_candidates": aliases,
                    "target_conflict": False,
                },
            )
        else:
            selection = TargetSurfaceSelection(
                requested_application=requested_application,
                status="target_ambiguous",
                foreground_scene=foreground_scene,
                target_scene=None,
                candidate_count=len(target_scenes),
                top_level_surfaces=tuple(summaries[:80]),
                requested_candidates=requested_candidates,
                resolved_candidates=(),
                target_conflict=True,
            )
    elif resolved:
        resolved.sort(
            key=lambda item: (
                item[1].get("source") == "model_tool_argument",
                item[1].get("exactness") is True,
                float(item[1].get("confidence") or 0.0),
                item[0].identity_strength,
            ),
            reverse=True,
        )
        selected, selected_authority = resolved[0]
        selection = TargetSurfaceSelection(
            **{
                **selected.__dict__,
                "requested_application": str(selected_authority["value"]),
                "requested_candidates": requested_candidates,
                "resolved_candidates": tuple(
                    dict(candidate)
                    for candidate_selection, candidate in resolved
                    if candidate_selection.target_scene is not None
                    and selected.target_scene is not None
                    and candidate_selection.target_scene.window_ref.ref_id
                    == selected.target_scene.window_ref.ref_id
                ),
                "target_conflict": False,
            },
        )
    else:
        conflict = len(requested_candidates) > 1
        ambiguous_count = max(
            (item[0].candidate_count for item in unresolved),
            default=0,
        )
        selection = TargetSurfaceSelection(
            requested_application=requested_application,
            status="target_ambiguous" if conflict else "target_missing",
            foreground_scene=foreground_scene,
            target_scene=None,
            candidate_count=ambiguous_count,
            top_level_surfaces=tuple(summaries[:80]),
            requested_candidates=requested_candidates,
            resolved_candidates=(),
            target_conflict=conflict,
        )
    if selection.target_scene is not None:
        _LAST_SCENE.set(selection.target_scene)
    return selection


def focus_existing_application_window(application: str) -> tuple[str, str]:
    """Focus one uniquely resolved existing app window without launching another."""

    if sys.platform != "win32":
        return "unavailable", ""
    selection = _select_desktop_target(application)
    scene = selection.target_scene
    if scene is None:
        return selection.status, ""
    if not (
        selection.identity_strength >= 350
        and (
            selection.identity_evidence.startswith("uia_content_application:")
            or selection.identity_evidence.startswith("executable_application:")
            or selection.identity_evidence.startswith("packaged_application_id:")
            or selection.identity_evidence.startswith("current_open_attempt:")
        )
    ):
        return "weak_identity", ""
    native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    if native is None:
        return "stale", ""
    detail = scene.window_ref.accessible_label
    if scene.foreground:
        _remember_launch_target(scene)
        return "ready", detail
    from osia.tools import desktop_control as dc

    try:
        status, focused_detail = dc.focus_app_window((
            native.native_window_id,
            detail,
            native.pid,
        ))
    except Exception:
        return "outcome_unknown", ""
    if status == "ready":
        _remember_launch_target(scene)
    return status, str(focused_detail or detail)


def _numeric_tokens(value: object) -> set[str]:
    tokens = set()
    for match in re.findall(
        r"(?<!\d)\d(?:[\s\u00a0\u202f.,']*\d)*(?!\d)",
        str(value or ""),
    ):
        normalized = re.sub(r"\D", "", match)
        if normalized:
            tokens.add(normalized)
    return tokens


def _contains_exact_integer(value: object, expected: str) -> bool:
    """Match an integer, allowing only unambiguous locale grouping."""

    for match in re.findall(
        r"(?<!\d)\d(?:[\s\u00a0\u202f.,']*\d)*(?!\d)",
        str(value or ""),
    ):
        compact = re.sub(r"[\s\u00a0\u202f']", "", match)
        if compact == expected:
            return True
        if len(expected) < 5:
            continue
        if re.fullmatch(
            r"\d{1,3}(?P<separator>[.,])\d{3}"
            r"(?:(?P=separator)\d{3})*",
            compact,
        ) and re.sub(r"[.,]", "", compact) == expected:
            return True
    return False


def rank_surface_candidates(
    surfaces: Iterable[DesktopSurfaceRef], target: str,
) -> tuple[DesktopSurfaceRef, ...]:
    query_text = str(target or "").lower().strip()
    query = {
        token for token in re.findall(r"[\w]+", query_text)
        if len(token) > 1
    }
    query_numbers = _numeric_tokens(query_text)
    ranked = []
    for surface in surfaces:
        if surface.surface_kind is SurfaceKind.WINDOW:
            continue
        label_tokens = set(re.findall(r"[\w]+", surface.accessible_label.lower()))
        numeric_match = bool(
            query_numbers
            and query_numbers.intersection(
                _numeric_tokens(surface.accessible_label),
            )
        )
        if query:
            overlap = len(query & label_tokens)
            exact = surface.accessible_label.lower().strip() == query_text
            if not overlap and not exact and not numeric_match:
                continue
        else:
            overlap, exact = 0, False
        score = (
            int(exact) * 100
            + int(numeric_match) * 80
            + overlap * 10
            + int(surface.enabled) * 2
            + int(surface.focused)
            + int(surface.selected)
        )
        ranked.append((score, surface))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[1] for item in ranked)


def _exact_observed_postcondition(
    scene: DesktopScene,
    target: str,
) -> tuple[str, str]:
    """Return the active required postcondition only for exact UIA state."""

    target_text = str(target or "").strip()
    if not target_text:
        return "", ""
    try:
        from osia.execution_strategy import (
            current_execution_strategy,
            is_desktop_visual_read_mission,
        )

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None:
        return "", ""
    if is_desktop_visual_read_mission(strategy):
        # An exact UIA label can prove that the application exists, but never
        # the appearance/content postcondition of an explicitly visual read.
        return "", ""
    frozen_attempt = (
        strategy.attempt_by_id(strategy._frozen_attempt_id)
        if strategy.frozen_reason else None
    )
    if (
        frozen_attempt is not None
        and frozen_attempt.attempted_effect == "application_open"
        and frozen_attempt.target_application
        and scene_matches_application(
            scene, frozen_attempt.target_application,
        )
    ):
        expected = _application_identity(
            frozen_attempt.target_application
        )
        requested = _application_identity(target_text)
        if expected and (requested == expected or expected in requested):
            return frozen_attempt.attempted_effect, "application_open"
    goal = (
        strategy.node_by_id(strategy._frozen_node_id)
        if strategy.frozen_reason else None
    ) or strategy.goal
    postcondition = str(goal.observable_success_postcondition or "")
    if (
        not postcondition
        or not scene_matches_application(scene, goal.required_application)
    ):
        return "", ""
    if goal.requested_action == "open":
        expected = _application_identity(goal.required_application)
        requested = _application_identity(target_text)
        if expected and (requested == expected or expected in requested):
            return postcondition, "application_open"
    if goal.requested_action in {"activate", "click", "select"}:
        normalized_target = " ".join(target_text.casefold().split())
        required_surface = " ".join(
            str(goal.required_surface or "").casefold().split()
        )
        target_object = " ".join(
            str(goal.target_object or "").casefold().split()
        )
        required_application = " ".join(
            str(goal.required_application or "").casefold().split()
        )
        if (
            not normalized_target
            or normalized_target == required_application
            or (
                required_surface
                and normalized_target != required_surface
                and required_surface not in normalized_target
            )
            or (
                not required_surface
                and normalized_target not in target_object
            )
        ):
            return "", ""
        for surface in scene.surfaces:
            if (
                surface.enabled
                and surface.accessible_label.strip().casefold()
                == normalized_target
                and (surface.selected or surface.focused)
            ):
                return postcondition, "exact_postcondition"
        return "", ""
    target_integer = re.sub(r"[\s\u00a0\u202f']", "", target_text)
    target_numbers = (
        {target_integer} if re.fullmatch(r"\d+", target_integer) else set()
    )
    if target_numbers:
        if not _contains_exact_integer(postcondition, target_integer):
            return "", ""
    elif target_text.lower() not in postcondition.lower():
        return "", ""
    for surface in scene.surfaces:
        if (
            not surface.enabled
            or surface.role not in {
                "DocumentControl", "EditControl", "PaneControl", "TextControl",
            }
        ):
            continue
        if target_numbers:
            label = surface.accessible_label.strip()
            label_is_number = bool(re.fullmatch(
                r"\d(?:[\s\u00a0\u202f.,']*\d)*",
                label,
            ))
            label_names_result = bool(re.search(
                r"\b(display|result|affichage|resultat|résultat)\b",
                label.lower(),
            ))
            if (
                (label_is_number or label_names_result)
                and _contains_exact_integer(label, target_integer)
            ):
                return postcondition, "exact_postcondition"
        elif surface.accessible_label.strip().lower() == target_text.lower():
            return postcondition, "exact_postcondition"
    return "", ""


def _exact_absent_postcondition(
    scene: DesktopScene,
    target: str,
) -> tuple[str, tuple[str, ...]]:
    """Prove a calculation absent only from a complete, neutral UIA state."""

    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None or not strategy.frozen_reason:
        return "", ()
    goal = strategy.node_by_id(strategy._frozen_node_id)
    if (
        goal is None
        or goal.requested_action != "calculate"
        or not scene.accessibility_complete
        or not scene.uia_tree_observed
        or not scene_matches_application(scene, goal.required_application)
    ):
        return "", ()
    postcondition = str(goal.observable_success_postcondition or "")
    target_integer = re.sub(
        r"[\s\u00a0\u202f']", "", str(target or "").strip(),
    )
    if (
        not postcondition
        or not re.fullmatch(r"\d+", target_integer)
        or not _contains_exact_integer(postcondition, target_integer)
    ):
        return "", ()
    state_labels = []
    for surface in scene.surfaces:
        if (
            not surface.enabled
            or surface.role not in {
                "DocumentControl", "EditControl", "PaneControl", "TextControl",
            }
        ):
            continue
        label = surface.accessible_label.strip()
        if not label:
            continue
        numeric_only = bool(re.fullmatch(
            r"\d(?:[\s\u00a0\u202f.,']*\d)*", label,
        ))
        names_state = bool(re.search(
            r"\b(display|result|output|input|equation|expression|"
            r"affichage|resultat|résultat)\b",
            label.lower(),
        ))
        if numeric_only or names_state:
            state_labels.append(label)
    if not state_labels:
        return "", ()
    observed_numbers = set().union(*(
        _numeric_tokens(label) for label in state_labels
    ))
    if target_integer in observed_numbers:
        return "", ()
    # A neutral zero-only state is the one generic calculation state that proves
    # both the requested result and every non-zero partial operand are absent.
    if not observed_numbers or any(number.strip("0") for number in observed_numbers):
        return "", ()
    if any(
        marker in label
        for label in state_labels
        for marker in ("+", "-", "−", "*", "×", "/", "÷", "=")
    ):
        return "", ()
    return postcondition, tuple(state_labels[:8])


def _primitive_state_projection(
    scene: DesktopScene,
    target: DesktopSurfaceRef | None = None,
    *,
    witness_surface_refs: Iterable[str] = (),
) -> dict:
    """Return bounded exact UIA state suitable for one primitive receipt."""

    state_rows = []
    for surface in scene.surfaces:
        if surface.role not in _PRIMITIVE_STATE_ROLES:
            continue
        label = surface.accessible_label.strip()
        if not label:
            continue
        identity = surface.runtime_signature or surface.ref_id
        state_rows.append({
            "surface_ref": surface.ref_id,
            "identity": identity,
            "role": surface.role,
            "label": label,
            "selected": bool(surface.selected),
            "focused": bool(surface.focused),
        })
    state_rows.sort(key=lambda item: (item["identity"], item["role"]))
    witness_refs = {
        str(item) for item in witness_surface_refs if str(item or "")
    }
    if witness_refs:
        witness_rows = [
            item for item in state_rows
            if item["surface_ref"] in witness_refs
        ][:_MAX_PRIMITIVE_STATE_ROWS]
        other_rows = [
            item for item in state_rows
            if item["surface_ref"] not in witness_refs
        ]
        state_rows = (
            witness_rows
            + other_rows[:max(
                0, _MAX_PRIMITIVE_STATE_ROWS - len(witness_rows),
            )]
        )
        state_rows.sort(key=lambda item: (item["identity"], item["role"]))
    else:
        state_rows = state_rows[:_MAX_PRIMITIVE_STATE_ROWS]
    encoded = json.dumps(
        state_rows,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = {
        "application": scene.application_id,
        "window_surface_ref": scene.window_ref.ref_id,
        "window_generation": scene.window_ref.window_generation,
        "target_foreground": bool(scene.foreground),
        "structural_epoch": scene.structural_epoch,
        "observed_at": scene.observed_at,
        "state_signature": hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24],
        "state_rows": state_rows,
    }
    if target is not None:
        payload["target"] = target.to_safe_dict()
    return payload


def _normalized_surface_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _surface_text_matches(observed: object, expected: object) -> bool:
    """Match one declared exact accessible-state value, never a fuzzy label."""

    expected_text = " ".join(str(expected or "").split())
    observed_text = " ".join(str(observed or "").split())
    if not expected_text or not observed_text:
        return False
    expected_integer = re.sub(r"[\s\u00a0\u202f']", "", expected_text)
    if re.fullmatch(r"\d+", expected_integer):
        return _contains_exact_integer(observed_text, expected_integer)
    return _normalized_surface_text(observed_text) == _normalized_surface_text(
        expected_text,
    )


def _stale_primitive_continuity(
    scene: DesktopScene,
    attempt,
    before: dict,
    selection: TargetSurfaceSelection | None,
) -> tuple[DesktopSurfaceRef | None, dict]:
    """Rebind one stale UIA target only through unique structured identity."""

    primitive = (
        attempt.primitive_receipt
        if isinstance(attempt.primitive_receipt, dict) else {}
    )
    if primitive.get("kind") != "desktop_uia_primitive":
        return None, {}
    identity = (
        attempt.target_surface_identity
        if isinstance(attempt.target_surface_identity, dict) else {}
    )
    target_before = before.get("target")
    target_before = target_before if isinstance(target_before, dict) else {}
    signature = str(
        identity.get("runtime_signature")
        or target_before.get("runtime_signature")
        or ""
    )
    role = str(identity.get("role") or target_before.get("role") or "")
    expected_kind = str(identity.get("surface_kind") or "")
    expected_patterns = {
        str(item) for item in (
            identity.get("patterns") or target_before.get("patterns") or []
        ) if str(item)
    }
    prior_surface = str(
        target_before.get("surface_ref") or identity.get("surface_ref") or ""
    )
    prior_window = str(before.get("window_surface_ref") or "")
    prior_generation = str(before.get("window_generation") or "")
    raw_prior_epoch = before.get("structural_epoch")
    prior_epoch_valid = bool(
        isinstance(raw_prior_epoch, int)
        and not isinstance(raw_prior_epoch, bool)
        and raw_prior_epoch >= 0
    )
    prior_epoch = int(raw_prior_epoch) if prior_epoch_valid else 0
    same_generation_epoch_advanced = bool(
        prior_epoch_valid
        and scene.window_ref.window_generation == prior_generation
        and scene.structural_epoch > prior_epoch
    )
    strong_evidence = bool(
        selection is not None
        and selection.status == "exact_target"
        and selection.target_scene is scene
        and selection.candidate_count == 1
        and selection.identity_strength >= 350
        and str(selection.identity_evidence or "").startswith((
            "uia_content_application:",
            "executable_application:",
            "packaged_application_id:",
            "current_open_attempt:",
        ))
    )
    if (
        not strong_evidence
        or not signature
        or not role
        or not prior_surface
        or attempt.target_surface != prior_surface
        or str(identity.get("window_surface_ref") or prior_window) != prior_window
        or str(identity.get("window_generation") or prior_generation)
        != prior_generation
        or (
            scene.window_ref.window_generation == prior_generation
            and not same_generation_epoch_advanced
        )
        or scene.window_ref.ref_id == prior_window
        or (
            identity.get("process_ref")
            and scene.window_ref.process_ref
            != str(identity.get("process_ref") or "")
        )
    ):
        return None, {}
    candidates = [
        surface for surface in scene.surfaces
        if surface.enabled
        and surface.application_id == scene.application_id
        and surface.process_ref == scene.window_ref.process_ref
        and surface.window_generation == scene.window_ref.window_generation
        and surface.runtime_signature == signature
        and surface.role == role
        and (not expected_kind or surface.surface_kind.value == expected_kind)
        and (
            not expected_patterns
            or expected_patterns.issubset(set(surface.patterns))
        )
    ]
    if len(candidates) != 1:
        return None, {}
    target = candidates[0]
    return target, {
        "surface_continuity": "semantic_reresolution_proven",
        "previous_window_surface_ref": prior_window,
        "previous_window_generation": prior_generation,
        "rebound_window_surface_ref": scene.window_ref.ref_id,
        "rebound_window_generation": scene.window_ref.window_generation,
        "rebound_target_surface_ref": target.ref_id,
        "previous_structural_epoch": prior_epoch,
        "rebound_structural_epoch": scene.structural_epoch,
    }


def _exact_primitive_reconciliation(
    scene: DesktopScene,
    *,
    selection: TargetSurfaceSelection | None = None,
) -> dict:
    """Prove one UIA substep only from a fresh attributable state delta."""

    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None or not strategy.frozen_reason:
        return {}
    attempt = strategy.attempt_by_id(strategy._frozen_attempt_id)
    primitive = (
        attempt.primitive_receipt
        if attempt is not None and isinstance(attempt.primitive_receipt, dict)
        else {}
    )
    if (
        attempt is None
        or primitive.get("kind") not in {
            "desktop_uia_primitive", "desktop_window_focus",
        }
        or (
            primitive.get("kind") == "desktop_uia_primitive"
            and not attempt.substep_id
        )
    ):
        return {}
    before = primitive.get("before")
    before = before if isinstance(before, dict) else {}
    dispatched_at = max(
        float(attempt.dispatched_at or 0.0),
        float(primitive.get("dispatched_at") or 0.0),
    )
    if dispatched_at <= 0.0 or scene.observed_at < dispatched_at:
        return {}
    if _application_identity(scene.application_id) != _application_identity(
        before.get("application"),
    ):
        return {}

    primitive_kind = str(primitive.get("kind") or "")
    target_before = before.get("target")
    target_before = target_before if isinstance(target_before, dict) else {}
    target_signature = str(target_before.get("runtime_signature") or "")
    target_surface_ref = str(target_before.get("surface_ref") or "")
    target_now = next(
        (
            surface for surface in scene.surfaces
            if target_signature
            and surface.runtime_signature == target_signature
            and (
                not target_surface_ref
                or surface.ref_id == target_surface_ref
            )
        ),
        None,
    )
    expected_window = str(before.get("window_surface_ref") or "")
    expected_generation = str(before.get("window_generation") or "")
    continuity = {}
    if (
        scene.window_ref.ref_id == expected_window
        and scene.window_ref.window_generation == expected_generation
    ):
        expected_target = str(
            target_before.get("surface_ref") or attempt.target_surface or ""
        )
        if (
            primitive_kind == "desktop_uia_primitive"
            and (
                target_now is None
                or not expected_target
                or target_now.ref_id != expected_target
            )
        ):
            return {}
        continuity = {
            "surface_continuity": "exact_same_generation",
            "previous_window_surface_ref": expected_window,
            "previous_window_generation": expected_generation,
            "rebound_window_surface_ref": scene.window_ref.ref_id,
            "rebound_window_generation": scene.window_ref.window_generation,
            "rebound_target_surface_ref": (
                target_now.ref_id if target_now is not None else ""
            ),
        }
    else:
        target_now, continuity = _stale_primitive_continuity(
            scene, attempt, before, selection,
        )
        if target_now is None:
            return {}
    try:
        from osia.execution_strategy import normalize_desktop_observable_effect

        declared_effect = normalize_desktop_observable_effect(
            attempt.observable_effect,
        )
        receipt_effect = normalize_desktop_observable_effect(
            primitive.get("observable_effect"),
        )
    except Exception:
        declared_effect = {}
        receipt_effect = {}
    after = _primitive_state_projection(
        scene,
        target_now,
        witness_surface_refs=(declared_effect.get("surface_ref") or "",),
    )
    before_rows = {
        str(item.get("identity") or ""): item
        for item in before.get("state_rows") or []
        if isinstance(item, dict) and item.get("identity")
    }
    after_rows = {
        str(item.get("identity") or ""): item
        for item in after.get("state_rows") or []
        if isinstance(item, dict) and item.get("identity")
    }
    common = set(before_rows).intersection(after_rows)
    changed_state = [
        identity for identity in sorted(common)
        if any(
            before_rows[identity].get(field) != after_rows[identity].get(field)
            for field in ("label", "selected")
        )
    ]
    mechanism = str(primitive.get("mechanism") or "")
    effect_identity = next((
        str(item.get("identity") or "")
        for item in before_rows.values()
        if item.get("surface_ref") == declared_effect.get("surface_ref")
    ), "")
    before_effect = before_rows.get(effect_identity) if effect_identity else None
    after_effect = after_rows.get(effect_identity) if effect_identity else None
    exact_declared_effect = bool(
        mechanism in {
            "invoke", "selectionitem", "toggle", "expandcollapse",
        }
        and declared_effect
        and receipt_effect == declared_effect
        and isinstance(before_effect, dict)
        and isinstance(after_effect, dict)
        and _normalized_surface_text(before_effect.get("label"))
        != _normalized_surface_text(after_effect.get("label"))
        and not _surface_text_matches(
            before_effect.get("label"), declared_effect.get("expected_text"),
        )
        and _surface_text_matches(
            after_effect.get("label"), declared_effect.get("expected_text"),
        )
    )
    target_state_changed = bool(
        target_now is not None
        and target_before.get("selected") != target_now.selected
    )
    selection_mechanism = mechanism in {
        "selectionitem", "toggle", "expandcollapse",
    }
    focus_completed = bool(
        primitive_kind == "desktop_window_focus"
        and before.get("target_foreground") is False
        and scene.foreground
        and scene.window_ref.focused
    )
    exact_local_effect = bool(
        focus_completed
        or exact_declared_effect
        or (
            selection_mechanism
            and not declared_effect
            and target_state_changed
        )
    )
    if not exact_local_effect:
        before_signature = str(before.get("state_signature") or "")
        target_unchanged = bool(
            target_now is not None
            and str(target_before.get("label") or "")
            == target_now.accessible_label
            and target_before.get("selected") is target_now.selected
            and target_before.get("focused") is target_now.focused
            and target_before.get("enabled") is target_now.enabled
            and tuple(target_before.get("patterns") or ())
            == tuple(target_now.patterns)
        )
        exact_no_partial_effect = bool(
            primitive_kind == "desktop_uia_primitive"
            and mechanism in {
                "invoke", "selectionitem", "toggle", "expandcollapse",
            }
            and declared_effect
            and receipt_effect == declared_effect
            and primitive.get("worker_may_be_active") is not True
            and continuity.get("surface_continuity")
            == "exact_same_generation"
            and scene.observed_at - dispatched_at
            >= _PRIMITIVE_ABSENCE_SETTLE_SECONDS
            and before.get("target_foreground") is scene.foreground
            and before.get("structural_epoch") == scene.structural_epoch
            and before_signature
            and after.get("state_signature") == before_signature
            and not changed_state
            and target_unchanged
        )
        if exact_no_partial_effect:
            return {
                "schema_version": 1,
                "evidence_kind": "exact_independent_desktop_state",
                "evidence_source": "inspect_desktop_surfaces",
                "dispatch_state": "effect_absent",
                "outcome": "confirmed_absence",
                "reason_code": (
                    "exact_desktop_primitive_absent_no_partial_effect"
                ),
                "semantic_postcondition": "desktop_uia_primitive_absent",
                "postcondition": attempt.primitive_postcondition,
                "composite_id": attempt.composite_id,
                "substep_id": attempt.substep_id,
                "substep_index": attempt.substep_index,
                "strategy_attempt_id": attempt.attempt_id,
                "semantic_node_id": attempt.node_id,
                "mechanism_id": attempt.mechanism_id,
                "attempted_effect": attempt.attempted_effect,
                "primitive_postcondition": attempt.primitive_postcondition,
                "observable_effect": dict(declared_effect),
                "observed_application": scene.application_id,
                "observed_window_surface_ref": scene.window_ref.ref_id,
                "window_generation": scene.window_ref.window_generation,
                "structural_epoch": scene.structural_epoch,
                "observed_at": scene.observed_at,
                "changed_state_count": 0,
                "target_state_changed": False,
                "target_foreground": bool(scene.foreground),
                "no_partial_effect": True,
                **continuity,
            }
        return {}
    return {
        "schema_version": 1,
        "evidence_kind": "exact_independent_desktop_state",
        "evidence_source": "inspect_desktop_surfaces",
        "dispatch_state": "effect_confirmed",
        "outcome": "confirmed_success",
        "reason_code": (
            "exact_desktop_target_focus_observed"
            if focus_completed else
            "exact_desktop_observable_effect_change"
            if exact_declared_effect else
            "exact_desktop_primitive_state_change"
        ),
        "semantic_postcondition": (
            "application_foreground"
            if focus_completed else
            declared_effect.get("kind")
            if exact_declared_effect else
            "desktop_uia_primitive_completed"
        ),
        "postcondition": (
            attempt.attempted_effect
            if focus_completed else
            attempt.primitive_postcondition
        ),
        "composite_id": attempt.composite_id,
        "substep_id": attempt.substep_id,
        "substep_index": attempt.substep_index,
        "strategy_attempt_id": attempt.attempt_id,
        "semantic_node_id": attempt.node_id,
        "mechanism_id": attempt.mechanism_id,
        "attempted_effect": attempt.attempted_effect,
        "primitive_postcondition": attempt.primitive_postcondition,
        "observable_effect": dict(declared_effect),
        "observed_application": scene.application_id,
        "observed_window_surface_ref": scene.window_ref.ref_id,
        "window_generation": scene.window_ref.window_generation,
        "structural_epoch": scene.structural_epoch,
        "observed_at": scene.observed_at,
        "changed_state_count": len(changed_state),
        "target_state_changed": bool(target_state_changed or focus_completed),
        "target_foreground": bool(scene.foreground),
        **continuity,
    }


def inspect_desktop_surfaces(target: str = "") -> dict:
    selection = _select_desktop_target(target)
    scene = selection.target_scene
    foreground_scene = selection.foreground_scene
    if scene is None and selection.status == "desktop_surface_unavailable":
        return {
            "text": "ERROR: exact foreground desktop surface is unavailable.",
            "receipt": {"reason_code": "desktop_surface_unavailable"},
        }
    if scene is None:
        foreground_payload = (
            foreground_scene.safe_dict(target="")
            if foreground_scene is not None else {}
        )
        scene_payload = {
            "application": "",
            "requested_application": selection.requested_application,
            "target_resolution": selection.status,
            "target_foreground": False,
            "target_identity_strength": selection.identity_strength,
            "target_candidate_count": selection.candidate_count,
            "accessibility_quality": selection.status,
            "window_surface_ref": "",
            "window_generation": "",
            "structural_epoch": 0,
            "foreground_application": foreground_payload.get("application", ""),
            "foreground_window_surface_ref": foreground_payload.get(
                "window_surface_ref", "",
            ),
            "foreground_accessibility_quality": foreground_payload.get(
                "accessibility_quality", "",
            ),
            "top_level_surfaces": list(selection.top_level_surfaces),
            "requested_target_candidates": list(
                selection.requested_candidates
            ),
            "resolved_target_candidates": list(
                selection.resolved_candidates
            ),
            "target_conflict": selection.target_conflict,
            "surfaces": [],
        }
        reason_code = (
            "semantic_target_surface_ambiguous"
            if selection.status == "target_ambiguous" else
            "semantic_target_surface_not_found"
        )
        projection = {
            "instruction": (
                "The requested application has no unique exact desktop surface. "
                "Do not use foreground accessibility or Vision as target evidence."
            ),
            "scene": scene_payload,
        }
        return {
            "text": json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "scene": scene_payload,
            "receipt": {
                "outcome": "observed",
                "reason_code": reason_code,
                "requested_application": selection.requested_application,
                "target_resolution": selection.status,
                "target_candidate_count": selection.candidate_count,
                "target_foreground": False,
                "target_conflict": selection.target_conflict,
                "foreground_application": scene_payload[
                    "foreground_application"
                ],
            },
        }
    payload = scene.safe_dict(target=target)
    payload.update({
        "requested_application": selection.requested_application,
        "target_resolution": selection.status,
        "target_identity_strength": selection.identity_strength,
        "target_identity_evidence": selection.identity_evidence,
        "target_candidate_count": selection.candidate_count,
        "target_foreground": bool(scene.foreground),
        "foreground_application": (
            foreground_scene.application_id if foreground_scene is not None else ""
        ),
        "foreground_window_surface_ref": (
            foreground_scene.window_ref.ref_id
            if foreground_scene is not None else ""
        ),
        "foreground_accessibility_quality": (
            foreground_scene.accessibility_quality(target).value
            if foreground_scene is not None else ""
        ),
        "top_level_surfaces": list(selection.top_level_surfaces),
        "requested_target_candidates": list(selection.requested_candidates),
        "resolved_target_candidates": list(selection.resolved_candidates),
        "target_conflict": selection.target_conflict,
    })
    model_projection = {
        "contract": (
            "READ_ONLY semantic desktop state bound to the reported application, "
            "window generation, and opaque surface references. It is structured "
            "accessibility evidence, not pixel evidence or action authority. "
            "A missing item is UNKNOWN unless claim-level evidence explicitly "
            "reports ABSENT_VERIFIED with complete relevant coverage."
        ),
        "scene": payload,
    }
    exact_postcondition, semantic_postcondition = _exact_observed_postcondition(
        scene, target,
    )
    receipt = {
        "outcome": "observed",
        "reason_code": "semantic_desktop_surface_observed",
        "window_surface_ref": scene.window_ref.ref_id,
        "structural_epoch": scene.structural_epoch,
        "observed_at": scene.observed_at,
        "requested_application": selection.requested_application,
        "target_resolution": selection.status,
        "target_identity_evidence": selection.identity_evidence,
        "target_foreground": bool(scene.foreground),
        "target_conflict": selection.target_conflict,
        "resolved_target_candidates": list(selection.resolved_candidates),
        "foreground_application": payload["foreground_application"],
    }
    if exact_postcondition:
        receipt.update({
            "schema_version": 1,
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "dispatch_state": "effect_confirmed",
            "outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "observed_target": str(target or "").strip(),
            "observed_application": scene.application_id,
            "observed_process_ref": scene.window_ref.process_ref,
            "observed_window_surface_ref": scene.window_ref.ref_id,
            "window_generation": scene.window_ref.window_generation,
            "semantic_postcondition": semantic_postcondition,
            "observed_state": semantic_postcondition,
            "postcondition": exact_postcondition,
        })
    else:
        absent_postcondition, neutral_state = _exact_absent_postcondition(
            scene, target,
        )
        if absent_postcondition:
            receipt.update({
                "schema_version": 1,
                "dispatch_state": "effect_absent",
                "outcome": "confirmed_absence",
                "reason_code": "exact_postcondition_absent_no_partial_effect",
                "observed_target": str(target or "").strip(),
                "postcondition": absent_postcondition,
                "no_partial_effect": True,
                "neutral_state": list(neutral_state),
            })
        else:
            primitive_reconciliation = _exact_primitive_reconciliation(
                scene,
                selection=selection,
            )
            if primitive_reconciliation:
                receipt.update(primitive_reconciliation)
    return {
        "text": json.dumps(
            model_projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "scene": payload,
        "receipt": receipt,
    }


def _capture_bounds(bounds: SurfaceBounds) -> bytes:
    return _capture_bounds_timed(bounds)[0]


def _capture_bounds_timed(bounds: SurfaceBounds) -> tuple[bytes, float, float]:
    if not bounds.valid():
        raise RuntimeError("invalid_surface_bounds")
    from PIL import Image, ImageGrab

    _ensure_thread_physical_pixels()
    capture_started = time.perf_counter()
    image = ImageGrab.grab(
        bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom),
        all_screens=True,
    )
    capture_latency_ms = (time.perf_counter() - capture_started) * 1000.0
    preparation_started = time.perf_counter()
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    preparation_latency_ms = (time.perf_counter() - preparation_started) * 1000.0
    return output.getvalue(), capture_latency_ms, preparation_latency_ms


class _DisplayConfigLuid(ctypes.Structure):
    _fields_ = [
        ("LowPart", wintypes.DWORD),
        ("HighPart", wintypes.LONG),
    ]


class _DisplayConfigRational(ctypes.Structure):
    _fields_ = [
        ("Numerator", ctypes.c_uint32),
        ("Denominator", ctypes.c_uint32),
    ]


class _DisplayConfigPathSourceInfo(ctypes.Structure):
    _fields_ = [
        ("adapterId", _DisplayConfigLuid),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("statusFlags", ctypes.c_uint32),
    ]


class _DisplayConfigPathTargetInfo(ctypes.Structure):
    _fields_ = [
        ("adapterId", _DisplayConfigLuid),
        ("id", ctypes.c_uint32),
        ("modeInfoIdx", ctypes.c_uint32),
        ("outputTechnology", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("scaling", ctypes.c_uint32),
        ("refreshRate", _DisplayConfigRational),
        ("scanLineOrdering", ctypes.c_uint32),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", ctypes.c_uint32),
    ]


class _DisplayConfigPathInfo(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", _DisplayConfigPathSourceInfo),
        ("targetInfo", _DisplayConfigPathTargetInfo),
        ("flags", ctypes.c_uint32),
    ]


class _DisplayConfig2DRegion(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_uint32), ("cy", ctypes.c_uint32)]


class _DisplayConfigVideoSignalInfo(ctypes.Structure):
    _fields_ = [
        ("pixelRate", ctypes.c_uint64),
        ("hSyncFreq", _DisplayConfigRational),
        ("vSyncFreq", _DisplayConfigRational),
        ("activeSize", _DisplayConfig2DRegion),
        ("totalSize", _DisplayConfig2DRegion),
        ("videoStandard", ctypes.c_uint32),
        ("scanLineOrdering", ctypes.c_uint32),
    ]


class _DisplayConfigTargetMode(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", _DisplayConfigVideoSignalInfo)]


class _DisplayConfigPointL(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _DisplayConfigSourceMode(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelFormat", ctypes.c_uint32),
        ("position", _DisplayConfigPointL),
    ]


class _DisplayConfigDesktopImageInfo(ctypes.Structure):
    _fields_ = [
        ("pathSourceSize", _DisplayConfig2DRegion),
        ("desktopImageRegion", wintypes.RECT),
        ("desktopImageClip", wintypes.RECT),
    ]


class _DisplayConfigModeUnion(ctypes.Union):
    _fields_ = [
        ("targetMode", _DisplayConfigTargetMode),
        ("sourceMode", _DisplayConfigSourceMode),
        ("desktopImageInfo", _DisplayConfigDesktopImageInfo),
    ]


class _DisplayConfigModeInfo(ctypes.Structure):
    _anonymous_ = ("mode",)
    _fields_ = [
        ("infoType", ctypes.c_uint32),
        ("id", ctypes.c_uint32),
        ("adapterId", _DisplayConfigLuid),
        ("mode", _DisplayConfigModeUnion),
    ]


class _DisplayConfigDeviceInfoHeader(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("adapterId", _DisplayConfigLuid),
        ("id", ctypes.c_uint32),
    ]


class _DisplayConfigSourceDeviceName(ctypes.Structure):
    _fields_ = [
        ("header", _DisplayConfigDeviceInfoHeader),
        ("viewGdiDeviceName", ctypes.c_wchar * 32),
    ]


class _DisplayConfigAdvancedColorInfo(ctypes.Structure):
    _fields_ = [
        ("header", _DisplayConfigDeviceInfoHeader),
        ("value", ctypes.c_uint32),
        ("colorEncoding", ctypes.c_uint32),
        ("bitsPerColorChannel", ctypes.c_uint32),
    ]


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def _displayconfig_advanced_color_enabled(
    device_name: str,
    user32,
) -> bool | None:
    """Read Advanced Color state for one active GDI display source."""

    get_sizes = user32.GetDisplayConfigBufferSizes
    query = user32.QueryDisplayConfig
    get_info = user32.DisplayConfigGetDeviceInfo
    get_sizes.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_sizes.restype = wintypes.LONG
    query.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_DisplayConfigPathInfo),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(_DisplayConfigModeInfo),
        ctypes.c_void_p,
    ]
    query.restype = wintypes.LONG
    get_info.argtypes = [ctypes.POINTER(_DisplayConfigDeviceInfoHeader)]
    get_info.restype = wintypes.LONG

    wanted = str(device_name or "").strip().casefold()
    if not wanted:
        return None
    active_paths = 0x00000002  # QDC_ONLY_ACTIVE_PATHS
    insufficient_buffer = 122
    for _attempt in range(3):
        path_count = ctypes.c_uint32(0)
        mode_count = ctypes.c_uint32(0)
        if get_sizes(
            active_paths, ctypes.byref(path_count), ctypes.byref(mode_count),
        ) != 0:
            return None
        if path_count.value <= 0 or mode_count.value <= 0:
            return None
        paths = (_DisplayConfigPathInfo * path_count.value)()
        modes = (_DisplayConfigModeInfo * mode_count.value)()
        result = query(
            active_paths,
            ctypes.byref(path_count),
            paths,
            ctypes.byref(mode_count),
            modes,
            None,
        )
        if result == insufficient_buffer:
            continue
        if result != 0:
            return None

        matched = 0
        queried = 0
        for path in paths[:path_count.value]:
            source_name = _DisplayConfigSourceDeviceName()
            source_name.header.type = 1  # GET_SOURCE_NAME
            source_name.header.size = ctypes.sizeof(source_name)
            source_name.header.adapterId = path.sourceInfo.adapterId
            source_name.header.id = path.sourceInfo.id
            if get_info(ctypes.byref(source_name.header)) != 0:
                continue
            if str(source_name.viewGdiDeviceName).strip().casefold() != wanted:
                continue
            matched += 1
            color = _DisplayConfigAdvancedColorInfo()
            color.header.type = 9  # GET_ADVANCED_COLOR_INFO
            color.header.size = ctypes.sizeof(color)
            color.header.adapterId = path.targetInfo.adapterId
            color.header.id = path.targetInfo.id
            if get_info(ctypes.byref(color.header)) != 0:
                continue
            queried += 1
            if color.value & 0x2:  # advancedColorEnabled
                return True
        if matched and queried == matched:
            return False
        return None
    return None


def _window_advanced_color_enabled(native_window_id: int) -> bool | None:
    """Return the target monitor's active Advanced Color/HDR state."""

    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        monitor_from_window = user32.MonitorFromWindow
        get_monitor_info = user32.GetMonitorInfoW
        monitor_from_window.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        monitor_from_window.restype = ctypes.c_void_p
        get_monitor_info.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_MonitorInfoExW),
        ]
        get_monitor_info.restype = wintypes.BOOL
        monitor = monitor_from_window(
            int(native_window_id), 2,  # MONITOR_DEFAULTTONEAREST
        )
        if not monitor:
            return None
        monitor_info = _MonitorInfoExW()
        monitor_info.cbSize = ctypes.sizeof(monitor_info)
        if not get_monitor_info(monitor, ctypes.byref(monitor_info)):
            return None
        return _displayconfig_advanced_color_enabled(
            monitor_info.szDevice, user32,
        )
    except Exception:
        return None


def _capture_window_graphics_image(native_window_id: int):
    """Capture one stabilized HWND frame through Windows Graphics Capture."""

    advanced_color = _window_advanced_color_enabled(native_window_id)
    if advanced_color is True:
        raise RuntimeError(
            "windows_graphics_capture_advanced_color_unsupported"
        )
    if advanced_color is None:
        raise RuntimeError(
            "windows_graphics_capture_advanced_color_state_unavailable"
        )
    from PIL import Image
    from windows_capture import WindowsCapture

    ready = threading.Event()
    captured: dict[str, object] = {"frames": [], "invalid_frames": 0}
    capture = WindowsCapture(
        cursor_capture=False,
        window_hwnd=int(native_window_id),
    )

    @capture.event
    def on_frame_arrived(frame, capture_control):
        try:
            pixels = frame.frame_buffer.copy()
            shape = getattr(pixels, "shape", ())
            if (
                len(shape) != 3
                or int(shape[2]) < 4
                or int(shape[0]) <= 0
                or int(shape[1]) <= 0
            ):
                captured["invalid_frames"] = int(
                    captured.get("invalid_frames") or 0
                ) + 1
                return
            if str(getattr(pixels, "dtype", "")) != "uint8":
                captured["unsupported_pixel_format"] = str(
                    getattr(pixels, "dtype", "unknown")
                )[:40]
                captured["invalid_frames"] = int(
                    captured.get("invalid_frames") or 0
                ) + 1
                return
            quality = _pixel_frame_quality(pixels[:, :, :3])
            if quality[0] <= 0.0:
                captured["invalid_frames"] = int(
                    captured.get("invalid_frames") or 0
                ) + 1
                return
            now = time.monotonic()
            captured_at = time.time()
            frames = captured["frames"]
            frames.append((pixels, quality, now, captured_at))
            if len(frames) > 12:
                del frames[:-12]
            if not captured.get("first_visible_at"):
                captured["first_visible_at"] = now
            same_geometry = sum(
                item[0].shape == pixels.shape for item in frames
            )
            if (
                same_geometry >= 2
                and now - float(captured["first_visible_at"]) >= 0.18
            ):
                captured["stabilized"] = True
                capture_control.stop()
                ready.set()
        except Exception as exc:
            captured["error"] = exc

    @capture.event
    def on_closed():
        if not captured.get("stabilized"):
            captured["target_closed"] = True
        ready.set()

    control = capture.start_free_threaded()
    frame_arrived = ready.wait(1.5)
    control.stop()
    shutdown_deadline = time.monotonic() + 0.5
    while not control.is_finished() and time.monotonic() < shutdown_deadline:
        time.sleep(0.005)
    if not control.is_finished():
        raise RuntimeError("windows_graphics_capture_shutdown_timeout")
    # ``is_finished`` is only a state observation. Join the native capture
    # thread before this session's Python/native owners can be released or a
    # later exact-window snapshot creates another WGC session.
    control.wait()
    frames = list(captured.get("frames") or [])
    if captured.get("unsupported_pixel_format"):
        raise RuntimeError(
            "windows_graphics_capture_hdr_format_unsupported"
        )
    if captured.get("target_closed"):
        raise RuntimeError("windows_graphics_capture_target_closed")
    if not frame_arrived and not frames:
        raise RuntimeError("windows_graphics_capture_frame_unavailable")
    stable_shape = tuple(int(value) for value in frames[-1][0].shape)
    stable_frames = [
        item for item in frames
        if tuple(int(value) for value in item[0].shape) == stable_shape
    ]
    if not stable_frames or (len(frames) > 1 and len(stable_frames) < 2):
        raise RuntimeError("windows_graphics_capture_geometry_unstable")
    pixels, quality, captured_stamp, captured_at = stable_frames[-1]
    shape = getattr(pixels, "shape", ())
    if len(shape) != 3 or int(shape[2]) < 4:
        raise RuntimeError("windows_graphics_capture_frame_invalid")
    height, width = int(shape[0]), int(shape[1])
    if width <= 0 or height <= 0:
        raise RuntimeError("windows_graphics_capture_frame_empty")
    image = Image.frombytes(
        "RGB",
        (width, height),
        pixels[:, :, :4].tobytes(),
        "raw",
        "BGRX",
        0,
        1,
    )
    return image, {
        "source_pixel_format": "BGRA8_UNORM",
        "pixel_format_conversion": "BGRX_TO_RGB8",
        "alpha_handling": "COMPOSITOR_ALPHA_IGNORED",
        "source_color_space": "WINDOWS_COMPOSITOR_SDR_UNSPECIFIED",
        "color_space_conversion": "NO_EXPLICIT_ICC_CONVERSION",
        "stabilized_frame_count": len(stable_frames),
        "invalid_frame_count": int(captured.get("invalid_frames") or 0),
        "visible_pixel_ratio": round(float(quality[0]), 6),
        "luma_spread": round(float(quality[1]), 3),
        "edge_energy": round(float(quality[2]), 3),
        "content_band_count": int(quality[3]),
        "frame_captured_monotonic": round(float(captured_stamp), 9),
        "frame_captured_at": float(captured_at),
    }


class PersistentWgcWindowCapture:
    """One exact-HWND WGC owner providing successive owned snapshots."""

    def __init__(self, native_window_id: int) -> None:
        if sys.platform != "win32":
            raise RuntimeError("persistent_wgc_requires_win32")
        self.native_window_id = int(native_window_id)
        if self.native_window_id <= 0:
            raise ValueError("persistent_wgc_hwnd_required")
        advanced_color = _window_advanced_color_enabled(self.native_window_id)
        if advanced_color is True:
            raise RuntimeError(
                "windows_graphics_capture_advanced_color_unsupported"
            )
        if advanced_color is None:
            raise RuntimeError(
                "windows_graphics_capture_advanced_color_state_unavailable"
            )
        self._condition = threading.Condition()
        self._capture = None
        self._control = None
        self._latest = None
        self._sequence = 0
        self._last_snapshot_sequence = 0
        self._invalid_frame_count = 0
        self._target_closed = False
        self._closed = False
        self._capture_start_count = 0
        self._capture_stop_count = 0

    def _start(self) -> None:
        if self._control is not None:
            return
        if self._closed:
            raise RuntimeError("persistent_wgc_session_closed")
        from windows_capture import WindowsCapture

        capture = WindowsCapture(
            cursor_capture=False,
            dirty_region=False,
            window_hwnd=self.native_window_id,
        )

        @capture.event
        def on_frame_arrived(frame, _capture_control):
            try:
                pixels = frame.frame_buffer.copy()
                shape = getattr(pixels, "shape", ())
                if (
                    len(shape) != 3
                    or int(shape[0]) <= 0
                    or int(shape[1]) <= 0
                    or int(shape[2]) < 4
                    or str(getattr(pixels, "dtype", "")) != "uint8"
                ):
                    raise RuntimeError("persistent_wgc_frame_invalid")
                quality = _pixel_frame_quality(pixels[:, :, :3])
                if quality[0] <= 0.0:
                    raise RuntimeError("persistent_wgc_frame_incomplete")
                captured_stamp = time.monotonic()
                captured_at = time.time()
            except Exception:
                with self._condition:
                    self._invalid_frame_count += 1
                    self._condition.notify_all()
                return
            with self._condition:
                # ``pixels`` is an owned NumPy copy; no native mapped view
                # survives the callback boundary.
                self._latest = (
                    pixels,
                    quality,
                    captured_stamp,
                    captured_at,
                )
                self._sequence += 1
                self._condition.notify_all()

        @capture.event
        def on_closed():
            with self._condition:
                self._target_closed = True
                self._condition.notify_all()

        self._capture = capture
        self._capture_start_count += 1
        self._control = capture.start_free_threaded()

    def snapshot(self, bounds: SurfaceBounds):
        """Return the latest exact pixels after draining a newer frame if any."""

        self._start()
        deadline = time.monotonic() + 1.5
        with self._condition:
            while self._latest is None and not self._target_closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("persistent_wgc_frame_unavailable")
                self._condition.wait(remaining)
            if self._target_closed:
                raise RuntimeError("windows_graphics_capture_target_closed")
            prior = self._last_snapshot_sequence
            if prior and self._sequence <= prior:
                # The native session remains alive between snapshots. A newer
                # dirty frame wins; no event means WGC continuously observed no
                # pixel update, so the owned latest frame remains authoritative.
                self._condition.wait_for(
                    lambda: self._sequence > prior or self._target_closed,
                    timeout=0.25,
                )
            if self._target_closed:
                raise RuntimeError("windows_graphics_capture_target_closed")
            pixels, quality, captured_stamp, frame_captured_at = self._latest
            sequence = int(self._sequence)
            unchanged = bool(prior and sequence <= prior)
            self._last_snapshot_sequence = sequence
            invalid_frame_count = int(self._invalid_frame_count)
        from PIL import Image

        height, width = int(pixels.shape[0]), int(pixels.shape[1])
        image = Image.frombytes(
            "RGB",
            (width, height),
            pixels[:, :, :4].tobytes(),
            "raw",
            "BGRX",
            0,
            1,
        )
        observed_at = time.time()
        provenance = {
            "source_pixel_format": "BGRA8_UNORM",
            "pixel_format_conversion": "BGRX_TO_RGB8",
            "alpha_handling": "COMPOSITOR_ALPHA_IGNORED",
            "source_color_space": "WINDOWS_COMPOSITOR_SDR_UNSPECIFIED",
            "color_space_conversion": "NO_EXPLICIT_ICC_CONVERSION",
            "stabilized_frame_count": 1,
            "invalid_frame_count": invalid_frame_count,
            "visible_pixel_ratio": round(float(quality[0]), 6),
            "luma_spread": round(float(quality[1]), 3),
            "edge_energy": round(float(quality[2]), 3),
            "content_band_count": int(quality[3]),
            "frame_captured_monotonic": round(float(captured_stamp), 9),
            "frame_captured_at": float(frame_captured_at),
            "observed_at": observed_at,
            "wgc_session_sequence": sequence,
            "wgc_capture_start_count": int(self._capture_start_count),
            "continuous_unchanged_observation": unchanged,
        }
        return _finalize_exact_window_capture(
            self.native_window_id,
            bounds,
            image,
            "win32_windows_graphics_capture_persistent",
            provenance,
            [],
        )

    def lifecycle_snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "native_window_id": str(self.native_window_id),
                "capture_start_count": int(self._capture_start_count),
                "capture_stop_count": int(self._capture_stop_count),
                "frame_sequence": int(self._sequence),
                "latest_frame_retained": self._latest is not None,
                "target_closed": bool(self._target_closed),
                "closed": bool(self._closed),
            }

    def wait_closed(self, timeout: float) -> bool:
        with self._condition:
            return bool(self._condition.wait_for(
                lambda: self._target_closed,
                timeout=max(0.0, float(timeout)),
            ))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        control = self._control
        capture = self._capture
        if control is not None:
            control.stop()
            control.wait()
            if not control.is_finished():
                raise RuntimeError("persistent_wgc_shutdown_incomplete")
            self._capture_stop_count += 1
        with self._condition:
            self._latest = None
            self._condition.notify_all()
        if capture is not None:
            capture.frame_handler = None
            capture.closed_handler = None
        self._control = None
        self._capture = None


def _capture_print_window_image(
    native_window_id: int,
    bounds: SurfaceBounds,
    *,
    flags: int,
):
    """Capture one HWND with the legacy GDI PrintWindow primitive."""

    if not _print_window_uses_physical_pixels(native_window_id):
        raise RuntimeError("printwindow_physical_mapping_unproven")
    from PIL import Image
    import ctypes
    import win32gui
    import win32ui

    window_dc = win32gui.GetWindowDC(int(native_window_id))
    if not window_dc:
        raise RuntimeError("exact_window_dc_unavailable")
    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, bounds.width, bounds.height)
        memory_dc.SelectObject(bitmap)
        rendered = ctypes.windll.user32.PrintWindow(
            int(native_window_id),
            memory_dc.GetSafeHdc(),
            int(flags),
        )
        if not rendered:
            raise RuntimeError("exact_window_print_failed")
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer(
            "RGB",
            (int(info["bmWidth"]), int(info["bmHeight"])),
            bits,
            "raw",
            "BGRX",
            0,
            1,
        ).copy()
    finally:
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if memory_dc is not None:
            try:
                memory_dc.DeleteDC()
            except Exception:
                pass
        if source_dc is not None:
            try:
                source_dc.DeleteDC()
            except Exception:
                pass
        win32gui.ReleaseDC(int(native_window_id), window_dc)


def _print_window_uses_physical_pixels(native_window_id: int) -> bool:
    """Whether PrintWindow coordinates are proven physical for this HWND."""

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        get_context = user32.GetWindowDpiAwarenessContext
        get_context.argtypes = [ctypes.c_void_p]
        get_context.restype = ctypes.c_void_p
        get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        get_awareness.argtypes = [ctypes.c_void_p]
        get_awareness.restype = ctypes.c_int
        context = get_context(int(native_window_id))
        # DPI_AWARENESS_PER_MONITOR_AWARE. System- and DPI-unaware targets can
        # render logical pixels into a physical-sized destination bitmap.
        return bool(context and int(get_awareness(context)) == 2)
    except Exception:
        return False


def _exact_capture_has_visible_pixels(image) -> bool:
    """Reject mechanically empty frames, not merely one non-black pixel."""

    if image.width <= 0 or image.height <= 0:
        return False
    converted = image.convert("RGB")
    sampled_image = converted.resize((
        min(160, converted.width), min(120, converted.height),
    ))
    histogram = list(
        sampled_image.get_flattened_data()
        if hasattr(sampled_image, "get_flattened_data") else
        sampled_image.getdata()
    )
    total = max(1, len(histogram))
    visible = sum(max(pixel) > 8 for pixel in histogram)
    height = min(120, converted.height)
    width = min(160, converted.width)
    pixels = histogram
    bands = sum(
        sum(
            max(pixels[y * width + x]) > 8
            for y in range(start, end)
            for x in range(width)
        ) / float(max(1, (end - start) * width)) >= 0.002
        for start, end in (
            (0, max(1, height // 3)),
            (height // 3, max(height // 3 + 1, 2 * height // 3)),
            (2 * height // 3, height),
        )
    )
    return (
        visible >= min(64, total)
        and visible / float(total) >= 0.002
        and bands >= 2
    )


def _pixel_frame_quality(bgr_pixels) -> tuple[float, float, float, int]:
    """Return bounded, application-agnostic frame quality metrics."""

    try:
        sampled = bgr_pixels[
            ::max(1, int(bgr_pixels.shape[0]) // 180),
            ::max(1, int(bgr_pixels.shape[1]) // 240),
            :3,
        ]
        visible = sampled.max(axis=2) > 8
        visible_count = int(visible.sum())
        total = max(1, int(visible.size))
        if visible_count < min(64, total) or visible_count / float(total) < 0.002:
            return (0.0, 0.0, 0.0, 0)
        band_count = sum(
            float(band.mean()) >= 0.002
            for band in (
                visible[:max(1, visible.shape[0] // 3), :],
                visible[
                    visible.shape[0] // 3:max(
                        visible.shape[0] // 3 + 1,
                        2 * visible.shape[0] // 3,
                    ),
                    :,
                ],
                visible[2 * visible.shape[0] // 3:, :],
            )
            if band.size
        )
        if band_count < 2:
            return (0.0, 0.0, 0.0, band_count)
        luminance = (
            sampled[:, :, 2].astype("float32") * 0.2126
            + sampled[:, :, 1].astype("float32") * 0.7152
            + sampled[:, :, 0].astype("float32") * 0.0722
        )
        spread = float(luminance.std())
        horizontal = float(abs(luminance[:, 1:] - luminance[:, :-1]).mean())
        vertical = float(abs(luminance[1:, :] - luminance[:-1, :]).mean())
        return (
            visible_count / float(total),
            spread,
            horizontal + vertical,
            band_count,
        )
    except Exception:
        return (0.0, 0.0, 0.0, 0)


def _finalize_exact_window_capture(
    native_window_id: int,
    bounds: SurfaceBounds,
    image,
    capture_method: str,
    provenance: Mapping[str, Any] | None,
    attempts: list[dict],
):
    """Validate one exact-HWND image and attach physical mapping metadata."""

    image = image.convert("RGB")
    current_bounds = _window_bounds(native_window_id)
    if current_bounds is None:
        raise RuntimeError("target_closed_during_capture")
    if (
        (
            current_bounds.left,
            current_bounds.top,
            current_bounds.right,
            current_bounds.bottom,
        ) != (bounds.left, bounds.top, bounds.right, bounds.bottom)
    ):
        raise RuntimeError("resize_during_capture")
    if not _exact_capture_has_visible_pixels(image):
        raise RuntimeError("capture_frame_incomplete")
    client_bounds = _window_client_bounds(native_window_id)
    capture_extent = str((provenance or {}).get("capture_extent") or "auto")

    def geometry_error(candidate: SurfaceBounds | None) -> float:
        if candidate is None or not candidate.valid():
            return float("inf")
        scale_x = candidate.width / float(image.width)
        scale_y = candidate.height / float(image.height)
        if not (0.25 <= scale_x <= 4.0 and 0.25 <= scale_y <= 4.0):
            return float("inf")
        return abs(scale_x - scale_y) / max(scale_x, scale_y)

    if capture_extent == "client_content" and client_bounds is not None:
        captured_bounds = client_bounds
    elif capture_extent == "full_window":
        captured_bounds = bounds
    else:
        window_error = geometry_error(bounds)
        client_error = geometry_error(client_bounds)
        captured_bounds = (
            client_bounds
            if client_bounds is not None and client_error + 0.01 < window_error
            else bounds
        )
        capture_extent = (
            "client_content" if captured_bounds is client_bounds
            else "full_window"
        )
    attempts.append({
        "capture_driver": capture_method,
        "outcome": "selected",
        "reason_code": "capture_frame_stable",
    })
    return image, capture_method, {
        **dict(provenance or {}),
        "capture_extent": capture_extent,
        "capture_screen_bounds": [
            captured_bounds.left,
            captured_bounds.top,
            captured_bounds.right,
            captured_bounds.bottom,
        ],
        "source_to_physical_scale": [
            round(captured_bounds.width / float(image.width), 6),
            round(captured_bounds.height / float(image.height), 6),
        ],
        "capture_attempts": attempts,
        "fallback_used": len(attempts) > 1,
        "captured_at": float(
            (provenance or {}).get("observed_at") or time.time()
        ),
    }


def _capture_exact_window_image(
    native_window_id: int,
    bounds: SurfaceBounds,
):
    """Capture one Win32 window without pixels from overlapping windows."""

    from PIL import ImageGrab

    candidates = (
        (
            "win32_windows_graphics_capture",
            lambda: _capture_window_graphics_image(native_window_id),
        ),
        (
            "win32_pillow_exact_window",
            lambda: (
                ImageGrab.grab(window=int(native_window_id)).convert("RGB"),
                {
                    "source_pixel_format": "PIL_RGB8",
                    "pixel_format_conversion": "PIL_TO_RGB8",
                    "alpha_handling": "OPAQUE_RGB",
                    "source_color_space": "UNPROFILED_SDR",
                    "color_space_conversion": "NO_EXPLICIT_ICC_CONVERSION",
                    "capture_extent": "client_content",
                },
            ),
        ),
        (
            "win32_printwindow_full",
            lambda: (
                _capture_print_window_image(
                    native_window_id, bounds, flags=2,
                ),
                {
                    "source_pixel_format": "BGRX8_GDI",
                    "pixel_format_conversion": "BGRX_TO_RGB8",
                    "alpha_handling": "NO_ALPHA_CHANNEL",
                    "source_color_space": "GDI_SDR_UNSPECIFIED",
                    "color_space_conversion": "NO_EXPLICIT_ICC_CONVERSION",
                    "capture_extent": "full_window",
                },
            ),
        ),
        (
            "win32_printwindow",
            lambda: (
                _capture_print_window_image(
                    native_window_id, bounds, flags=0,
                ),
                {
                    "source_pixel_format": "BGRX8_GDI",
                    "pixel_format_conversion": "BGRX_TO_RGB8",
                    "alpha_handling": "NO_ALPHA_CHANNEL",
                    "source_color_space": "GDI_SDR_UNSPECIFIED",
                    "color_space_conversion": "NO_EXPLICIT_ICC_CONVERSION",
                    "capture_extent": "full_window",
                },
            ),
        ),
    )
    attempts = []
    for capture_method, capture_candidate in candidates:
        try:
            image, provenance = capture_candidate()
            return _finalize_exact_window_capture(
                native_window_id,
                bounds,
                image,
                capture_method,
                provenance,
                attempts,
            )
        except RuntimeError as exc:
            if str(exc) == "resize_during_capture":
                raise
            attempts.append({
                "capture_driver": capture_method,
                "outcome": "rejected",
                "reason_code": (
                    str(exc)[:80]
                    if re.fullmatch(r"[a-z0-9_]{1,80}", str(exc)) else
                    type(exc).__name__
                ),
            })
            if str(exc) == "windows_graphics_capture_hdr_format_unsupported":
                raise _ExactWindowCaptureUnsupported(attempts) from exc
            continue
        except Exception as exc:
            attempts.append({
                "capture_driver": capture_method,
                "outcome": "rejected",
                "reason_code": type(exc).__name__[:80],
            })
            continue
    raise _ExactWindowCaptureFailure(attempts)


class _ExactWindowCaptureFailure(RuntimeError):
    """Typed, sanitized provenance when every exact HWND driver is rejected."""

    def __init__(self, attempts: Iterable[dict]):
        super().__init__("exact_window_capture_black_frame")
        self.attempts = tuple(
            dict(item) for item in attempts if isinstance(item, dict)
        )[:8]


class _ExactWindowCaptureUnsupported(RuntimeError):
    """Fail closed when exact-window color data cannot be normalized safely."""

    def __init__(self, attempts: Iterable[dict]):
        super().__init__("windows_hdr_exact_capture_unsupported")
        self.attempts = tuple(
            dict(item) for item in attempts if isinstance(item, dict)
        )[:8]


class _ProtectedWindowCapture(RuntimeError):
    """The target explicitly asked Windows to exclude its pixels."""

    def __init__(self, protection_kind: str):
        super().__init__("capture_protected_surface")
        self.protection_kind = str(protection_kind or "capture_restricted")


def _visual_target_for_scene(
    scene: DesktopScene,
    requested: DesktopSurfaceRef,
) -> VisualTarget:
    window_bounds = scene.window_ref.bounds
    requested_bounds = requested.bounds or window_bounds
    if window_bounds is None or requested_bounds is None:
        raise VisionCaptureError(CaptureFailure.WRONG_DIMENSIONS)
    native = DESKTOP_SURFACE_REGISTRY.native_identity(scene.window_ref.ref_id)
    if native is None:
        raise VisionCaptureError(CaptureFailure.STALE_TARGET)
    bounded = (
        max(requested_bounds.left, window_bounds.left),
        max(requested_bounds.top, window_bounds.top),
        min(requested_bounds.right, window_bounds.right),
        min(requested_bounds.bottom, window_bounds.bottom),
    )
    return VisualTarget(
        application_id=scene.application_id,
        domain=VisionDomain.DESKTOP,
        surface_id=requested.ref_id,
        surface_generation=scene.window_ref.window_generation,
        bounds=bounded,
        visible=not scene.minimized,
        minimized=scene.minimized,
        native_window_id=str(native.native_window_id),
        monitor_id=(
            f"physical:{window_bounds.left}:{window_bounds.top}:"
            f"{window_bounds.right}:{window_bounds.bottom}"
        ),
        dpi_scale=window_bounds.dpi_scale,
    )


def _capture_bound_visual_surface(
    scene: DesktopScene,
    requested: DesktopSurfaceRef,
    *,
    provider: str,
    model: str,
):
    """Capture the proven surface and bind its full coordinate transform."""

    from PIL import ImageGrab
    from osia.vision_runtime import CoordinateTransform, prepare_capture

    window_bounds = scene.window_ref.bounds
    requested_bounds = requested.bounds or window_bounds
    if (
        window_bounds is None
        or requested_bounds is None
        or not window_bounds.valid()
        or not requested_bounds.valid()
    ):
        raise RuntimeError("scoped_capture_bounds_unavailable")
    native = DESKTOP_SURFACE_REGISTRY.native_identity(scene.window_ref.ref_id)
    if native is None:
        raise RuntimeError("native_window_identity_unavailable")

    bounded = SurfaceBounds(
        max(requested_bounds.left, window_bounds.left),
        max(requested_bounds.top, window_bounds.top),
        min(requested_bounds.right, window_bounds.right),
        min(requested_bounds.bottom, window_bounds.bottom),
        dpi_scale=window_bounds.dpi_scale,
    )
    if not bounded.valid():
        raise RuntimeError("scoped_capture_bounds_unavailable")

    _ensure_thread_physical_pixels()
    exact_surface_pixels = False
    capture_method = "foreground_visible_region"
    source_width = bounded.width
    source_height = bounded.height
    source_box = (0.0, 0.0, float(source_width), float(source_height))
    capture_provenance = {
        "source_pixel_format": "PIL_RGB8",
        "pixel_format_conversion": "PIL_TO_RGB8",
        "alpha_handling": "OPAQUE_RGB",
        "source_color_space": "UNPROFILED_SDR",
        "color_space_conversion": "NO_EXPLICIT_ICC_CONVERSION",
        "capture_attempts": [],
        "fallback_used": False,
        "captured_at": time.time(),
    }
    screen_bounds = (
        bounded.left, bounded.top, bounded.right, bounded.bottom,
    )
    if sys.platform == "win32" and isinstance(native.native_window_id, int):
        protection_kind = _window_capture_protection_reason(
            native.native_window_id,
        )
        if protection_kind:
            raise _ProtectedWindowCapture(protection_kind)
        image, capture_method, capture_provenance = _capture_exact_window_image(
            native.native_window_id, window_bounds,
        )
        if image.width <= 0 or image.height <= 0:
            raise RuntimeError("exact_window_capture_empty")
        raw_capture_bounds = capture_provenance.get("capture_screen_bounds")
        if (
            not isinstance(raw_capture_bounds, (list, tuple))
            or len(raw_capture_bounds) != 4
        ):
            raise RuntimeError("exact_window_capture_bounds_unavailable")
        capture_bounds = SurfaceBounds(
            *(int(value) for value in raw_capture_bounds),
            dpi_scale=window_bounds.dpi_scale,
        )
        if (
            not capture_bounds.valid()
            or capture_bounds.left < window_bounds.left - 8
            or capture_bounds.top < window_bounds.top - 8
            or capture_bounds.right > window_bounds.right + 8
            or capture_bounds.bottom > window_bounds.bottom + 8
        ):
            raise RuntimeError("exact_window_capture_bounds_invalid")
        captured_region = SurfaceBounds(
            max(bounded.left, capture_bounds.left),
            max(bounded.top, capture_bounds.top),
            min(bounded.right, capture_bounds.right),
            min(bounded.bottom, capture_bounds.bottom),
            dpi_scale=window_bounds.dpi_scale,
        )
        if not captured_region.valid():
            raise RuntimeError("exact_window_crop_empty")
        source_width, source_height = image.size
        scale_x = source_width / float(capture_bounds.width)
        scale_y = source_height / float(capture_bounds.height)
        crop_box = (
            max(0, int(math.floor(
                (captured_region.left - capture_bounds.left) * scale_x
            ))),
            max(0, int(math.floor(
                (captured_region.top - capture_bounds.top) * scale_y
            ))),
            min(source_width, int(math.ceil(
                (captured_region.right - capture_bounds.left) * scale_x
            ))),
            min(source_height, int(math.ceil(
                (captured_region.bottom - capture_bounds.top) * scale_y
            ))),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            raise RuntimeError("exact_window_crop_empty")
        source_box = tuple(float(value) for value in crop_box)
        image = image.crop(crop_box)
        exact_surface_pixels = True
        screen_bounds = (
            capture_bounds.left,
            capture_bounds.top,
            capture_bounds.right,
            capture_bounds.bottom,
        )
    else:
        image = ImageGrab.grab(
            bbox=(bounded.left, bounded.top, bounded.right, bounded.bottom),
            all_screens=True,
        ).convert("RGB")
        capture_provenance["captured_at"] = time.time()

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    native_token = hashlib.sha256(
        f"{native.pid}:{native.native_window_id}".encode(
            "utf-8", errors="replace",
        )
    ).hexdigest()[:24]
    current_client_bounds = _window_client_bounds(native.native_window_id)
    transform = CoordinateTransform(
        source_width=source_width,
        source_height=source_height,
        source_box=source_box,
        screen_bounds=screen_bounds,
        client_origin_screen=_window_client_origin(
            native.native_window_id,
            (window_bounds.left, window_bounds.top),
        ),
        dpi_scale=window_bounds.dpi_scale,
        monitor_id=(
            f"physical:{window_bounds.left}:{window_bounds.top}:"
            f"{window_bounds.right}:{window_bounds.bottom}"
        ),
        window_bounds=(
            window_bounds.left, window_bounds.top,
            window_bounds.right, window_bounds.bottom,
        ),
        client_bounds=(
            0, 0,
            max(1, (
                current_client_bounds.width
                if current_client_bounds is not None else window_bounds.width
            )),
            max(1, (
                current_client_bounds.height
                if current_client_bounds is not None else window_bounds.height
            )),
        ),
    )
    visual_target = _visual_target_for_scene(scene, requested)
    return prepare_capture(
        output.getvalue(),
        provider=provider,
        model=model,
        application_id=scene.application_id,
        surface_ref=requested.ref_id,
        window_generation=scene.window_ref.window_generation,
        structural_epoch=scene.structural_epoch,
        transform=transform,
        native_window_token=native_token,
        capture_method=capture_method,
        exact_surface_pixels=exact_surface_pixels,
        foreground=scene.foreground,
        domain=VisionDomain.DESKTOP,
        target_identity=visual_target.identity_key,
        monitor_id=visual_target.monitor_id,
        window_bounds=(
            window_bounds.left, window_bounds.top,
            window_bounds.right, window_bounds.bottom,
        ),
        client_bounds=(
            0, 0,
            max(1, (
                current_client_bounds.width
                if current_client_bounds is not None else window_bounds.width
            )),
            max(1, (
                current_client_bounds.height
                if current_client_bounds is not None else window_bounds.height
            )),
        ),
        screen_origin=(screen_bounds[0], screen_bounds[1]),
        captured_at=float(
            capture_provenance.get("captured_at") or time.time()
        ),
        source_pixel_format=str(
            capture_provenance.get("source_pixel_format") or ""
        ),
        pixel_format_conversion=str(
            capture_provenance.get("pixel_format_conversion") or ""
        ),
        alpha_handling=str(
            capture_provenance.get("alpha_handling") or ""
        ),
        source_color_space=str(
            capture_provenance.get("source_color_space") or ""
        ),
        color_space_conversion=str(
            capture_provenance.get("color_space_conversion") or ""
        ),
        capture_attempts=tuple(
            item for item in capture_provenance.get("capture_attempts", [])
            if isinstance(item, dict)
        ),
        fallback_used=capture_provenance.get("fallback_used") is True,
    )


def _capture_surface_coverage(
    scene: DesktopScene,
    requested: DesktopSurfaceRef,
    capture,
    selection: VisualSurfaceSelection,
    *,
    refinement_requested: bool,
) -> SurfaceCoverage:
    """Validate geometry/hierarchy only; never inspect screenshot semantics."""

    if refinement_requested:
        return SurfaceCoverage.REGION
    window_bounds = scene.window_ref.bounds
    requested_bounds = requested.bounds or window_bounds
    if (
        window_bounds is None
        or requested_bounds is None
        or capture.image_width <= 0
        or capture.image_height <= 0
    ):
        return SurfaceCoverage.UNKNOWN
    if requested.ref_id != scene.window_ref.ref_id:
        ratio = (
            requested_bounds.width * requested_bounds.height
        ) / float(max(1, window_bounds.width * window_bounds.height))
        return (
            SurfaceCoverage.CLIENT_CONTENT
            if ratio >= 0.40 else SurfaceCoverage.REGION
        )
    source_width = int(
        capture.transform.source_width
        if capture.transform is not None else capture.image_width
    )
    source_height = int(
        capture.transform.source_height
        if capture.transform is not None else capture.image_height
    )
    if source_width <= 0 or source_height <= 0:
        return SurfaceCoverage.UNKNOWN
    target_aspect = window_bounds.width / float(max(1, window_bounds.height))
    source_aspect = source_width / float(max(1, source_height))
    aspect_ratio = source_aspect / float(max(0.001, target_aspect))
    if aspect_ratio > 1.8 or aspect_ratio < 0.55:
        return SurfaceCoverage.CHROME_ONLY
    if selection.coverage in {
        SurfaceCoverage.FULL_WINDOW,
        SurfaceCoverage.CLIENT_CONTENT,
    }:
        return selection.coverage
    # Non-Windows deterministic fixtures and older native adapters still bind
    # an exact window directly; matching full-frame geometry is sufficient.
    if capture.exact_surface_pixels:
        return SurfaceCoverage.FULL_WINDOW
    return SurfaceCoverage.UNKNOWN


def _visual_full_window_required() -> bool:
    try:
        from osia.execution_strategy import (
            current_execution_strategy,
            is_desktop_visual_read_mission,
        )

        strategy = current_execution_strategy()
        return bool(
            strategy is not None
            and is_desktop_visual_read_mission(strategy)
        )
    except Exception:
        return False


def _record_surface_lifecycle(
    diagnostics: dict | None,
    phase: str,
    **metadata,
) -> None:
    if diagnostics is None:
        return
    events = diagnostics.setdefault("events", [])
    if isinstance(events, list):
        events.append({"phase": str(phase), **metadata})


def _diagnostic_bounds(bounds: SurfaceBounds | None) -> dict:
    return bounds.to_safe_dict() if bounds is not None else {}


def _refresh_windows_native_scene(
    native_window_id: object,
    *,
    target: str = "",
    expected_native: NativeSurfaceIdentity | None = None,
    expected_application: str = "",
    expected_window_generation: str = "",
    diagnostics: dict | None = None,
) -> tuple[DesktopScene | None, str]:
    """Validate and refresh one real HWND without bounded list enumeration."""

    if sys.platform != "win32":
        return None, "SURFACE_NOT_DISCOVERED"
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return None, "SURFACE_NOT_DISCOVERED"
        win32gui, _win32process = loaded
        hwnd = int(native_window_id)
    except (TypeError, ValueError, OverflowError):
        return None, "WINDOW_NOT_FOUND"
    except Exception:
        return None, "SURFACE_NOT_DISCOVERED"

    try:
        if not win32gui.IsWindow(hwnd):
            _record_surface_lifecycle(
                diagnostics, "FRESHNESS_VALIDATION",
                expected_hwnd=str(hwnd), current_hwnd=str(hwnd),
                is_window=False, is_window_visible=False,
                first_mismatch="is_window",
            )
            return None, "SURFACE_STALE_HWND_DESTROYED"
        visible = bool(win32gui.IsWindowVisible(hwnd))
        if not visible:
            return None, "WINDOW_NOT_VISIBLE"
    except Exception:
        return None, "WINDOW_NOT_FOUND"

    title, pid, class_name, bounds = _window_metadata(hwnd)
    if pid <= 0:
        return None, "PROCESS_IDENTITY_FAILED"
    if bounds is None and not _window_is_minimized(hwnd):
        return None, "SURFACE_NOT_DISCOVERED"
    application_id, process_started_at = _process_details(pid)
    _record_surface_lifecycle(
        diagnostics, "NATIVE_REFRESH",
        hwnd=str(hwnd), pid=int(pid), window_class=str(class_name or "")[:160],
        process_started_at=round(float(process_started_at or 0.0), 6),
        application=str(application_id or "")[:160],
        bounds=_diagnostic_bounds(bounds),
        is_window=True, is_window_visible=visible,
    )
    if (
        not application_id
        or application_id.startswith("process-")
        or float(process_started_at or 0.0) <= 0.0
    ):
        return None, "PROCESS_IDENTITY_FAILED"
    if expected_native is not None:
        if int(pid) != int(expected_native.pid):
            _record_surface_lifecycle(
                diagnostics, "FRESHNESS_VALIDATION",
                expected_pid=int(expected_native.pid), current_pid=int(pid),
                expected_hwnd=str(expected_native.native_window_id),
                current_hwnd=str(hwnd), first_mismatch="process_id",
            )
            return None, "SURFACE_STALE_HWND_RECYCLED"
        if abs(
            float(process_started_at)
            - float(expected_native.process_started_at)
        ) > 0.05:
            _record_surface_lifecycle(
                diagnostics, "FRESHNESS_VALIDATION",
                expected_process_started_at=round(
                    float(expected_native.process_started_at), 6,
                ),
                current_process_started_at=round(
                    float(process_started_at), 6,
                ),
                first_mismatch="process_started_at",
            )
            return None, "SURFACE_STALE_PROCESS_CHANGED"
        if str(class_name or "") != str(expected_native.class_name or ""):
            _record_surface_lifecycle(
                diagnostics, "FRESHNESS_VALIDATION",
                expected_window_class=str(expected_native.class_name or "")[:160],
                current_window_class=str(class_name or "")[:160],
                first_mismatch="window_class",
            )
            return None, "SURFACE_STALE_IDENTITY_CHANGED"

    try:
        foreground = dc._foreground_window_win32()
        foreground_hwnd = _foreground_native_window_id(foreground)
        scene = _observe_window_scene(
            (hwnd, title, pid),
            target=target,
            foreground=bool(
                foreground_hwnd and int(foreground_hwnd) == hwnd
            ),
            class_name_hint=class_name,
        )
    except Exception:
        return None, "SURFACE_REGISTRATION_FAILED"
    if scene is None:
        return None, "SURFACE_REGISTRATION_FAILED"
    if expected_application and not _scene_matches_application(
        scene, expected_application,
    ):
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            expected_application=str(expected_application)[:160],
            current_application=str(scene.application_id or "")[:160],
            first_mismatch="application_id",
        )
        return None, "SURFACE_STALE_IDENTITY_CHANGED"
    if (
        expected_window_generation
        and scene.window_ref.window_generation != expected_window_generation
    ):
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            expected_generation=str(expected_window_generation)[:160],
            current_generation=str(scene.window_ref.window_generation)[:160],
            first_mismatch="window_generation",
        )
        return None, "SURFACE_STALE_GENERATION_MISMATCH"
    refreshed_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    if refreshed_native is None:
        return None, "SURFACE_REGISTRATION_FAILED"
    _record_surface_lifecycle(
        diagnostics, "CURRENT_REGISTRY_STATE",
        current_surface_id=scene.window_ref.ref_id,
        current_generation=scene.window_ref.window_generation,
        current_scene_epoch=int(scene.structural_epoch),
        current_bounds=_diagnostic_bounds(scene.window_ref.bounds),
    )
    return scene, ""


def _observe_exact_bound_visual_scene(
    surface_ref: str,
    *,
    target: str = "",
    expected_application: str = "",
    expected_window_generation: str = "",
    expected_native_window_id: str = "",
    diagnostics: dict | None = None,
) -> tuple[DesktopScene | None, DesktopSurfaceRef | None, str]:
    """Re-observe the exact native window already bound to an opaque ref."""

    ref_id = str(surface_ref or "")
    anchor = DESKTOP_SURFACE_REGISTRY.refresh_anchor(ref_id)
    if anchor.surface is None or anchor.native is None:
        # A newly discovered or structurally refreshed real window can be
        # represented safely when ActionTruth supplies its full stable identity.
        if (
            sys.platform == "win32"
            and str(expected_native_window_id or "")
            and expected_application
            and expected_window_generation
        ):
            scene, reason = _refresh_windows_native_scene(
                expected_native_window_id,
                target=target,
                expected_application=expected_application,
                expected_window_generation=expected_window_generation,
                diagnostics=diagnostics,
            )
            if scene is not None:
                _record_surface_lifecycle(
                    diagnostics, "RESOLVER_OUTPUT",
                    requested_surface_id=ref_id,
                    current_surface_id=scene.window_ref.ref_id,
                    current_generation=scene.window_ref.window_generation,
                    current_scene_epoch=int(scene.structural_epoch),
                    continuity="EXTERNAL_IDENTITY_REFRESHED",
                    first_mismatch="",
                )
                return scene, scene.window_ref, ""
            return None, None, reason
        reason = anchor.reason or "SURFACE_NOT_DISCOVERED"
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            requested_surface_id=ref_id,
            first_mismatch=reason,
        )
        return None, None, {
            "unknown_surface_ref": "SURFACE_NOT_DISCOVERED",
            "window_generation_changed": "SURFACE_STALE_GENERATION_MISMATCH",
            "accessibility_runtime_id_disappeared": (
                "SURFACE_STALE_IDENTITY_CHANGED"
            ),
        }.get(reason, "SURFACE_STALE_IDENTITY_CHANGED")

    initial_surface = anchor.surface
    native = anchor.native
    _record_surface_lifecycle(
        diagnostics, "RESOLVER_INPUT",
        surface_id=initial_surface.ref_id,
        stored_generation=initial_surface.window_generation,
        stored_scene_epoch=int(initial_surface.structural_epoch),
        stored_process_ref=initial_surface.process_ref,
        stored_bounds=_diagnostic_bounds(initial_surface.bounds),
        anchor_status=anchor.status,
        anchor_reason=anchor.reason,
        expected_hwnd=str(native.native_window_id),
        expected_pid=int(native.pid),
        expected_process_started_at=round(
            float(native.process_started_at), 6,
        ),
        expected_window_class=str(native.class_name or "")[:160],
    )
    if (
        str(expected_native_window_id or "")
        and str(native.native_window_id) != str(expected_native_window_id)
    ):
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            expected_hwnd=str(expected_native_window_id),
            current_hwnd=str(native.native_window_id),
            first_mismatch="native_window_id",
        )
        return None, None, "SURFACE_STALE_IDENTITY_CHANGED"
    if (
        expected_window_generation
        and expected_window_generation != initial_surface.window_generation
    ):
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            expected_generation=str(expected_window_generation)[:160],
            current_generation=initial_surface.window_generation,
            first_mismatch="window_generation",
        )
        return None, None, "SURFACE_STALE_GENERATION_MISMATCH"
    if (
        expected_application
        and not _surface_matches_application(
            initial_surface, expected_application,
        )
    ):
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            expected_application=str(expected_application)[:160],
            current_application=initial_surface.application_id,
            first_mismatch="application_id",
        )
        return None, None, "SURFACE_STALE_IDENTITY_CHANGED"

    if sys.platform == "win32":
        scene, reason = _refresh_windows_native_scene(
            native.native_window_id,
            target=target,
            expected_native=native,
            expected_application=(
                expected_application or initial_surface.application_id
            ),
            expected_window_generation=(
                expected_window_generation
                or initial_surface.window_generation
            ),
            diagnostics=diagnostics,
        )
        if scene is None:
            return None, None, reason
    else:
        scene = observe_foreground_scene(target=target)

    if scene is None:
        return None, None, "SURFACE_NOT_DISCOVERED"
    if (
        scene.window_ref.process_ref != initial_surface.process_ref
        or scene.window_ref.window_generation
        != (expected_window_generation or initial_surface.window_generation)
    ):
        mismatch = (
            "process_ref"
            if scene.window_ref.process_ref != initial_surface.process_ref else
            "window_generation"
        )
        _record_surface_lifecycle(
            diagnostics, "FRESHNESS_VALIDATION",
            expected_process_ref=initial_surface.process_ref,
            current_process_ref=scene.window_ref.process_ref,
            expected_generation=(
                expected_window_generation
                or initial_surface.window_generation
            ),
            current_generation=scene.window_ref.window_generation,
            first_mismatch=mismatch,
        )
        return None, None, (
            "SURFACE_STALE_PROCESS_CHANGED"
            if mismatch == "process_ref" else
            "SURFACE_STALE_GENERATION_MISMATCH"
        )
    refreshed_window_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    exact_window_continuity = bool(
        initial_surface.surface_kind in {
            SurfaceKind.WINDOW, SurfaceKind.DIALOG,
        }
        and refreshed_window_native is not None
        and int(refreshed_window_native.pid) == int(native.pid)
        and abs(
            float(refreshed_window_native.process_started_at)
            - float(native.process_started_at)
        ) <= 0.05
        and refreshed_window_native.native_window_id == native.native_window_id
        and refreshed_window_native.class_name == native.class_name
        and scene.window_ref.window_generation
        == initial_surface.window_generation
    )
    if exact_window_continuity:
        requested = scene.window_ref
        rebound_native = refreshed_window_native
        rebound_reason = "same_native_application_window_refreshed"
    else:
        rebound = DESKTOP_SURFACE_REGISTRY.rebind_same_target(
            ref_id, scene.surfaces,
        )
        if (
            not rebound.resolved
            or rebound.surface is None
            or rebound.native is None
        ):
            _record_surface_lifecycle(
                diagnostics, "FRESHNESS_VALIDATION",
                expected_surface_id=ref_id,
                current_window_surface_id=scene.window_ref.ref_id,
                expected_generation=initial_surface.window_generation,
                current_generation=scene.window_ref.window_generation,
                expected_scene_epoch=int(initial_surface.structural_epoch),
                current_scene_epoch=int(scene.structural_epoch),
                expected_bounds=_diagnostic_bounds(initial_surface.bounds),
                current_bounds=_diagnostic_bounds(scene.window_ref.bounds),
                first_mismatch=rebound.reason or "semantic_identity",
            )
            return None, None, "SURFACE_STALE_IDENTITY_CHANGED"
        requested = rebound.surface
        rebound_native = rebound.native
        rebound_reason = rebound.reason
    same_generation = bool(
        initial_surface.window_generation == requested.window_generation
    )
    _record_surface_lifecycle(
        diagnostics, "REGISTRATION_REFRESH",
        previous_surface_id=ref_id,
        current_surface_id=requested.ref_id,
        previous_generation=initial_surface.window_generation,
        current_generation=requested.window_generation,
        generation_change_reason=(
            "unchanged_same_native_identity"
            if same_generation else "native_identity_changed"
        ),
        scene_epoch_before=int(initial_surface.structural_epoch),
        scene_epoch_after=int(scene.structural_epoch),
        new_ref_created=bool(requested.ref_id != ref_id),
        representation_change_reason=(
            anchor.reason or rebound_reason or "stable_reobservation"
        ),
    )
    _record_surface_lifecycle(
        diagnostics, "FRESHNESS_VALIDATION",
        expected_surface_id=ref_id,
        current_surface_id=requested.ref_id,
        expected_generation=initial_surface.window_generation,
        current_generation=requested.window_generation,
        expected_scene_epoch=int(initial_surface.structural_epoch),
        current_scene_epoch=int(scene.structural_epoch),
        expected_hwnd=str(native.native_window_id),
        current_hwnd=str(rebound_native.native_window_id),
        expected_pid=int(native.pid),
        current_pid=int(rebound_native.pid),
        expected_process_started_at=round(
            float(native.process_started_at), 6,
        ),
        current_process_started_at=round(
            float(rebound_native.process_started_at), 6,
        ),
        expected_bounds=_diagnostic_bounds(initial_surface.bounds),
        current_bounds=_diagnostic_bounds(requested.bounds),
        first_mismatch="",
        continuity=(
            "SAME_TARGET_REFRESHED"
            if requested.ref_id != ref_id
            or requested.structural_epoch != initial_surface.structural_epoch
            or requested.bounds != initial_surface.bounds
            else "EXACT_CURRENT_TARGET"
        ),
    )
    _record_surface_lifecycle(
        diagnostics, "RESOLVER_OUTPUT",
        surface_id=requested.ref_id,
        generation=requested.window_generation,
        scene_epoch=int(scene.structural_epoch),
        target_identity="same_native_semantic_target",
    )
    return scene, requested, ""


def _vision_failure_result(
    *,
    trace: VisionTrace,
    stage: VisionStage,
    failure: VisionFailure,
    reason_code: str,
    text: str,
    surface_ref: str = "",
    **metadata,
) -> dict:
    trace.fail(stage, failure, reason_code)
    return {
        "text": "ERROR: " + text,
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "not_dispatched",
            "outcome": "confirmed_failure_before_effect",
            "reason_code": reason_code,
            "failure_class": failure.value,
            "failed_stage": stage.value,
            "surface_ref": str(surface_ref or ""),
            **metadata,
        },
        "diagnostics": trace.safe_dict(),
    }


def request_visual_observation(
    surface_ref: str,
    reason: str,
    target: str = "",
    *,
    visual_frame_id: str = "",
    visual_bbox: dict | None = None,
    expected_application: str = "",
    expected_window_generation: str = "",
    expected_native_window_id: str = "",
    _surface_diagnostics: bool = False,
) -> dict:
    """Capture an exact frame for the active model's normal reasoning loop."""

    trace = VisionTrace()
    provider, model = effective_model_image_route()
    if not provider or not model:
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.IMAGE_PART_BUILT,
            failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
            reason_code="IMAGE_INPUT_UNAVAILABLE",
            text=(
                "the active model has no proven image-input capability; "
                "no screenshot was captured or handed off."
            ),
            surface_ref=surface_ref,
        )

    surface_lifecycle = (
        {"schema_version": 1, "events": []}
        if _surface_diagnostics else None
    )
    bound_scene = _LAST_SCENE.get()
    scene, requested, resolution_reason = _observe_exact_bound_visual_scene(
        str(surface_ref or ""),
        target=target,
        expected_application=expected_application,
        expected_window_generation=expected_window_generation,
        expected_native_window_id=expected_native_window_id,
        diagnostics=surface_lifecycle,
    )
    if scene is None or requested is None:
        failure = (
            VisionFailure.TARGET_STALE
            if (
                resolution_reason.startswith("SURFACE_STALE")
                or resolution_reason == "GENERATION_MISMATCH"
            )
            else VisionFailure.TARGET_RESOLUTION_FAILED
        )
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.TARGET_RESOLVED,
            failure=failure,
            reason_code=resolution_reason or "TARGET_RESOLUTION_FAILED",
            text=(
                "the exact desktop target could not be resolved safely; "
                "no screenshot was captured or sent."
            ),
            surface_ref=surface_ref,
            provider_id=provider,
            model_id=model,
            **(
                {"surface_lifecycle": surface_lifecycle}
                if surface_lifecycle is not None else {}
            ),
        )
    if (
        bound_scene is not None
        and (
            scene.window_ref.process_ref != bound_scene.window_ref.process_ref
            or scene.window_ref.window_generation
            != bound_scene.window_ref.window_generation
        )
    ):
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.TARGET_RESOLVED,
            failure=VisionFailure.TARGET_RESOLUTION_FAILED,
            reason_code="TARGET_CONFLICT",
            text=(
                "the requested visual surface belongs to a different exact "
                "application target; no screenshot was captured or sent."
            ),
            surface_ref=surface_ref,
            provider_id=provider,
            model_id=model,
        )
    surface_candidates, surface_selection = (
        _visual_surface_selection_for_scene(scene)
    )
    current_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    if (
        sys.platform == "win32"
        and current_native is not None
        and surface_selection.capture is not None
        and int(surface_selection.capture.hwnd)
        != int(current_native.native_window_id)
    ):
        selected = surface_selection.capture
        corrected = _observe_window_scene(
            (selected.hwnd, selected.title, selected.pid),
            target=target,
            foreground=selected.foreground,
            class_name_hint=selected.class_name,
        )
        corrected_native = (
            DESKTOP_SURFACE_REGISTRY.native_identity(
                corrected.window_ref.ref_id,
            ) if corrected is not None else None
        )
        same_selected_native = bool(
            corrected_native is not None
            and int(corrected_native.native_window_id) == int(selected.hwnd)
            and int(corrected_native.pid) == int(selected.pid)
            and abs(
                float(corrected_native.process_started_at)
                - float(selected.process_started_at)
            ) <= 0.05
            and str(corrected_native.class_name or "")
            == str(selected.class_name or "")
        )
        same_semantic_application = bool(
            corrected is not None
            and (
                _scene_matches_application(
                    corrected,
                    expected_application or scene.application_id,
                )
                or (
                    _application_identity(
                        corrected.window_ref.accessible_label
                    )
                    == _application_identity(
                        scene.window_ref.accessible_label
                    )
                    and bool(scene.window_ref.accessible_label)
                )
            )
        )
        if (
            corrected is None
            or not same_selected_native
            or not same_semantic_application
        ):
            return _vision_failure_result(
                trace=trace,
                stage=VisionStage.TARGET_RESOLVED,
                failure=VisionFailure.TARGET_RESOLUTION_FAILED,
                reason_code="VISUAL_CONTENT_SURFACE_AMBIGUOUS",
                text=(
                    "the semantic application root/content surface could not "
                    "be selected safely; no image was sent."
                ),
                surface_ref=requested.ref_id,
            )
        scene = corrected
        requested = corrected.window_ref
        _LAST_SCENE.set(corrected)
        surface_candidates, surface_selection = (
            _visual_surface_selection_for_scene(scene)
        )
    if scene.minimized:
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.CAPTURE_REQUESTED,
            failure=VisionFailure.TARGET_STALE,
            reason_code="WINDOW_NOT_VISIBLE",
            text=(
                "the exact desktop window is minimized; no screenshot was "
                "captured or sent."
            ),
            surface_ref=surface_ref,
        )
    if (
        scene.application_id in _SECURE_DESKTOP_PROCESS_NAMES
        or any(
            item.accessible_label == "[sensitive field]"
            for item in scene.surfaces
        )
    ):
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.CAPTURE_REQUESTED,
            failure=VisionFailure.CAPTURE_ACCESS_DENIED,
            reason_code="CAPTURE_ACCESS_DENIED",
            text=(
                "the target is sensitive or secure; no screenshot was captured "
                "or sent."
            ),
            surface_ref=surface_ref,
        )
    try:
        observation_reason = ObservationReason(str(reason))
    except ValueError:
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.TARGET_RESOLVED,
            failure=VisionFailure.TARGET_RESOLUTION_FAILED,
            reason_code="TARGET_RESOLUTION_FAILED",
            text="unsupported visual observation reason.",
            surface_ref=surface_ref,
        )

    store = _SCENE_STORES.get()
    if store is None:
        store = VisualObservationStore()
        _SCENE_STORES.set(store)
    refinement_requested = bool(visual_frame_id or visual_bbox)
    if refinement_requested and not (
        str(visual_frame_id or "").strip() and isinstance(visual_bbox, dict)
    ):
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.REFINE_REQUESTED,
            failure=VisionFailure.TARGET_RESOLUTION_FAILED,
            reason_code="REFINE_INPUT_INVALID",
            text=(
                "visual refinement requires one current visual_frame_id and one "
                "normalized visual_bbox; no image was sent."
            ),
            surface_ref=requested.ref_id,
        )
    capture_kind = (
        CaptureKind.REGION
        if requested.ref_id != scene.window_ref.ref_id else CaptureKind.WINDOW
    )
    if refinement_requested:
        capture_kind = CaptureKind.ZOOM_REFINE
    if not store.budget_allows(capture_kind):
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.CAPTURE_REQUESTED,
            failure=VisionFailure.CAPTURE_UNSUPPORTED,
            reason_code="CAPTURE_BUDGET_EXHAUSTED",
            text="visual observation budget exhausted; no image was sent.",
            surface_ref=surface_ref,
        )

    runtime = VisionRuntime(provider, model)
    visual_target = _visual_target_for_scene(scene, requested)
    capture_metrics = {"latency_ms": 0.0}
    refinement_source_frame_id = ""
    content_coverage = SurfaceCoverage.UNKNOWN

    def _capture_driver(_target: VisualTarget):
        started = time.perf_counter()
        try:
            return _capture_bound_visual_surface(
                scene,
                requested,
                provider=provider,
                model=model,
            )
        finally:
            capture_metrics["latency_ms"] = (
                time.perf_counter() - started
            ) * 1000.0

    driver = (
        WindowsCaptureDriver(_capture_driver)
        if sys.platform == "win32" else _capture_driver
    )
    try:
        resolved_target, trace = runtime.resolve_target(
            target or requested.accessible_label,
            resolver=lambda _semantic: visual_target,
            trace=trace,
        )
        if refinement_requested:
            try:
                bbox = NormalizedBBox(
                    left=float(visual_bbox.get("left")),
                    top=float(visual_bbox.get("top")),
                    right=float(visual_bbox.get("right")),
                    bottom=float(visual_bbox.get("bottom")),
                )
            except (AttributeError, TypeError, ValueError, OverflowError):
                return _vision_failure_result(
                    trace=trace,
                    stage=VisionStage.REFINE_REQUESTED,
                    failure=VisionFailure.TARGET_RESOLUTION_FAILED,
                    reason_code="REFINE_BBOX_INVALID",
                    text="the normalized refinement region is invalid; no image was sent.",
                    surface_ref=requested.ref_id,
                )
            frames = _VISUAL_FRAMES.get() or {}
            source_capture = frames.get(str(visual_frame_id or ""))
            current_native = DESKTOP_SURFACE_REGISTRY.native_identity(
                scene.window_ref.ref_id,
            )
            current_native_token = (
                hashlib.sha256(
                    f"{current_native.pid}:{current_native.native_window_id}".encode(
                        "utf-8", errors="replace",
                    )
                ).hexdigest()[:24]
                if current_native is not None else ""
            )
            if (
                source_capture is None
                or source_capture.provider != provider
                or source_capture.model != model
                or source_capture.application_id != scene.application_id
                or source_capture.window_generation
                != scene.window_ref.window_generation
                or source_capture.structural_epoch != scene.structural_epoch
                or not source_capture.is_fresh()
                or not current_native_token
                or source_capture.native_window_token != current_native_token
            ):
                return _vision_failure_result(
                    trace=trace,
                    stage=VisionStage.REFINE_REQUESTED,
                    failure=VisionFailure.TARGET_STALE,
                    reason_code="REFINE_SOURCE_FRAME_STALE",
                    text=(
                        "the refinement frame is unavailable or belongs to another "
                        "target/model generation; no image was sent."
                    ),
                    surface_ref=requested.ref_id,
                    frame_id=str(visual_frame_id or ""),
                )
            pass_index = store.refinement_count + 1
            trace.record(
                VisionStage.REFINE_REQUESTED,
                "bounded_roi_refinement_requested",
                source_frame_id=source_capture.frame_id,
                pass_index=pass_index,
                roi=bbox.to_dict(),
            )
            capture = crop_capture(source_capture, bbox)
            if capture is source_capture:
                return _vision_failure_result(
                    trace=trace,
                    stage=VisionStage.REFINE_REQUESTED,
                    failure=VisionFailure.CAPTURE_UNSUPPORTED,
                    reason_code="REFINE_REGION_NOT_TIGHTER",
                    text=(
                        "the requested region would resend the unchanged full frame; "
                        "no image was sent."
                    ),
                    surface_ref=requested.ref_id,
                    frame_id=source_capture.frame_id,
                )
            refinement_source_frame_id = source_capture.frame_id
        else:
            capture, trace = runtime.capture(
                resolved_target,
                driver,
                trace=trace,
            )
        if not scene.foreground and not capture.exact_surface_pixels:
            raise VisionCaptureError(
                CaptureFailure.UNSUPPORTED,
                "background_capture_not_exact_target_bound",
            )
        content_coverage = _capture_surface_coverage(
            scene,
            requested,
            capture,
            surface_selection,
            refinement_requested=refinement_requested,
        )
        if (
            _visual_full_window_required()
            and not refinement_requested
            and requested.ref_id == scene.window_ref.ref_id
            and content_coverage not in {
                SurfaceCoverage.FULL_WINDOW,
                SurfaceCoverage.CLIENT_CONTENT,
            }
        ):
            raise VisionCaptureError(
                CaptureFailure.WRONG_DIMENSIONS,
                "CAPTURE_COVERAGE_INSUFFICIENT",
            )

        media_owner_id, media_turn_id = effective_model_media_scope()
        from osia.image_inputs import turn_scoped_media_owner

        scoped_media_owner = turn_scoped_media_owner(
            media_owner_id, media_turn_id,
        )
        request = ImageInputTransport().build(
            capture,
            (
                "Exact read-only UI frame attached for the active model. "
                "Use it only as evidence for the current goal."
            ),
            {},
            parent_image_id=None,
            owner_id=scoped_media_owner,
        )
        image_reference = next(
            (
                dict(part)
                for part in request.messages[0]["content"]
                if isinstance(part, dict) and part.get("type") == "image_ref"
            ),
            {},
        )
        if not image_reference.get("image_id"):
            raise VisionTransportError(
                VisualStatus.CAPTURE_INVALID,
                "image_reference_missing_after_registration",
                failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
                stage=VisionStage.IMAGE_PART_BUILT,
            )
        capture = replace(
            capture,
            registered_image_id=str(image_reference["image_id"]),
        )
        # Recreate the artifact only as an identity assertion; bytes stay in
        # the canonical media store and are not duplicated here.
        capture.to_image_artifact()
        trace.record(
            VisionStage.IMAGE_PART_BUILT,
            "active_model_image_part_built",
            provider_id=provider,
            model_id=model,
            frame_id=capture.frame_id,
            image_part_count=request.image_part_count,
        )
    except _ProtectedWindowCapture as exc:
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.CAPTURE_REQUESTED,
            failure=VisionFailure.CAPTURE_PROTECTED,
            reason_code="CAPTURE_PROTECTED",
            text=(
                "the application or Windows protects this exact target from "
                "capture; no image input was produced."
            ),
            surface_ref=requested.ref_id,
            protection_kind=exc.protection_kind,
        )
    except VisionTargetError as exc:
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.TARGET_RESOLVED,
            failure=VisionFailure.TARGET_RESOLUTION_FAILED,
            reason_code=exc.reason_code or "TARGET_RESOLUTION_FAILED",
            text="visual target resolution failed; no image was sent.",
            surface_ref=requested.ref_id,
        )
    except VisionCaptureError as exc:
        reason_codes = {
            CaptureFailure.ACCESS_DENIED: "CAPTURE_ACCESS_DENIED",
            CaptureFailure.PROTECTED_CONTENT: "CAPTURE_PROTECTED",
            CaptureFailure.MINIMIZED_TARGET: "WINDOW_NOT_VISIBLE",
            CaptureFailure.STALE_TARGET: "SURFACE_STALE",
            CaptureFailure.GENERATION_MISMATCH: "GENERATION_MISMATCH",
            CaptureFailure.RESIZED_DURING_CAPTURE: "SURFACE_STALE",
            CaptureFailure.EMPTY_FRAME: "CAPTURE_EMPTY",
            CaptureFailure.BLACK_FRAME: "CAPTURE_EMPTY",
            CaptureFailure.DEVICE_LOSS: "CAPTURE_DEVICE_FAILED",
            CaptureFailure.WRONG_DIMENSIONS: "CAPTURE_EMPTY",
            CaptureFailure.UNSUPPORTED: "CAPTURE_UNSUPPORTED",
        }
        reason_code = (
            "CAPTURE_COVERAGE_INSUFFICIENT"
            if exc.reason_code == "CAPTURE_COVERAGE_INSUFFICIENT" else
            reason_codes[exc.failure]
        )
        return _vision_failure_result(
            trace=trace,
            stage=(trace.failed_stage or VisionStage.CAPTURE_REQUESTED),
            failure={
                CaptureFailure.ACCESS_DENIED: VisionFailure.CAPTURE_ACCESS_DENIED,
                CaptureFailure.PROTECTED_CONTENT: VisionFailure.CAPTURE_PROTECTED,
                CaptureFailure.MINIMIZED_TARGET: VisionFailure.TARGET_STALE,
                CaptureFailure.STALE_TARGET: VisionFailure.TARGET_STALE,
                CaptureFailure.GENERATION_MISMATCH: VisionFailure.TARGET_STALE,
                CaptureFailure.RESIZED_DURING_CAPTURE: VisionFailure.TARGET_STALE,
                CaptureFailure.EMPTY_FRAME: VisionFailure.CAPTURE_EMPTY,
                CaptureFailure.BLACK_FRAME: VisionFailure.CAPTURE_EMPTY,
                CaptureFailure.DEVICE_LOSS: VisionFailure.CAPTURE_DEVICE_FAILED,
                CaptureFailure.WRONG_DIMENSIONS: VisionFailure.CAPTURE_DIMENSION_MISMATCH,
                CaptureFailure.UNSUPPORTED: VisionFailure.CAPTURE_UNSUPPORTED,
            }[exc.failure],
            reason_code=reason_code,
            text=(
                "exact desktop capture failed safely; no global capture or "
                "alternate model was used."
            ),
            surface_ref=requested.ref_id,
            capture_attempts=[
                dict(item) for item in getattr(
                    getattr(exc, "__cause__", None), "attempts", ()
                )
                if isinstance(item, dict)
            ],
        )
    except VisionTransportError as exc:
        return _vision_failure_result(
            trace=trace,
            stage=exc.stage,
            failure=exc.failure,
            reason_code=exc.reason_code,
            text=(
                "the exact image part could not be built for the active model; "
                "no alternate model was used."
            ),
            surface_ref=requested.ref_id,
        )
    except Exception:
        return _vision_failure_result(
            trace=trace,
            stage=VisionStage.IMAGE_PART_BUILT,
            failure=VisionFailure.IMAGE_PART_BUILD_FAILED,
            reason_code="IMAGE_PART_BUILD_FAILED",
            text=(
                "the scoped image input could not be prepared; no global "
                "capture or alternate model was used."
            ),
            surface_ref=requested.ref_id,
        )

    observation, is_new = store.add(
        surface_ref=requested.ref_id,
        capture_kind=capture_kind,
        reason=observation_reason,
        width=capture.image_width,
        height=capture.image_height,
        capture_epoch=store.capture_count,
        structural_epoch=scene.structural_epoch,
        surface_fingerprint=(
            f"{provider}:{model}:{scene.application_id}:"
            f"{scene.window_ref.window_generation}:{scene.structural_epoch}"
        ),
        png_bytes=capture.image_bytes,
    )
    safe = observation.to_safe_dict()
    safe.update({
        "capture_latency_ms": round(capture_metrics["latency_ms"], 3),
        "image_preparation_latency_ms": round(
            capture.preparation_latency_ms, 3,
        ),
        "new_image": is_new,
        "frame_id": capture.frame_id,
        "source_frame_id": refinement_source_frame_id,
        "refine_count": store.refinement_count,
        "content_coverage": content_coverage.value,
        "evidence_semantics": observer_evidence_semantics(
            EvidenceSource.VISION,
            EvidenceCoverage.PARTIAL,
            target_identity=(
                f"{scene.application_id}:{scene.window_ref.window_generation}"
            ),
            surface_id=requested.ref_id,
            frame_id=capture.frame_id,
        ),
    })
    frames = _VISUAL_FRAMES.get()
    if frames is None:
        frames = {}
        _VISUAL_FRAMES.set(frames)
    frames[capture.frame_id] = capture
    actuation_lease = register_visual_actuation_frame(capture)
    while len(frames) > 8:
        frames.pop(next(iter(frames)))

    final_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    result = {
        "text": json.dumps({
            "contract": (
                "READ_ONLY exact visual evidence attached to the active model's "
                "current reasoning loop. It is not action authority. Visible positive "
                "evidence may establish presence; an unreadable or omitted detail is "
                "UNKNOWN, not absent. If more detail is needed, refine this frame with "
                "one normalized bbox."
            ),
            "observation": safe,
            "target_identity": {
                "application": scene.application_id,
                "surface_id": requested.ref_id,
                "window_surface_id": scene.window_ref.ref_id,
                "window_generation": scene.window_ref.window_generation,
                "structural_epoch": scene.structural_epoch,
            },
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "image_ref": image_reference,
        "observation": safe,
        "scene": scene.safe_dict(target=target),
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "not_dispatched",
            "outcome": "observed",
            "reason_code": "ACTIVE_MODEL_IMAGE_PART_BUILT",
            "surface_ref": requested.ref_id,
            "window_surface_ref": scene.window_ref.ref_id,
            "application": scene.application_id,
            "window_generation": scene.window_ref.window_generation,
            "structural_epoch": scene.structural_epoch,
            "observed_at": scene.observed_at,
            "captured_at": capture.captured_at,
            "expires_at": capture.expires_at,
            "actuation_lease_state": actuation_lease["lease_state"],
            "actuation_lease_expires_at": actuation_lease["lease_expires_at"],
            "image_sha256": observation.image_sha256,
            "image_id": str(image_reference.get("image_id") or ""),
            "image_mime": str(image_reference.get("mime") or capture.mime),
            "image_width": int(image_reference.get("width") or capture.image_width),
            "image_height": int(image_reference.get("height") or capture.image_height),
            "native_window_token": capture.native_window_token,
            "native_window_id": str(
                final_native.native_window_id if final_native is not None else ""
            ),
            "capture_method": capture.capture_method,
            "capture_driver": capture.capture_method,
            "capture_attempts": [
                dict(item) for item in capture.capture_attempts
            ],
            "fallback_used": capture.fallback_used,
            "source_pixel_format": capture.source_pixel_format,
            "pixel_format_conversion": capture.pixel_format_conversion,
            "alpha_handling": capture.alpha_handling,
            "source_color_space": capture.source_color_space,
            "color_space_conversion": capture.color_space_conversion,
            "exact_surface_pixels": capture.exact_surface_pixels,
            "target_foreground": bool(scene.foreground),
            "frame_id": capture.frame_id,
            "surface_generation": capture.surface_generation,
            "provider_id": provider,
            "model_id": model,
            "image_part_count": request.image_part_count,
            "dimensions": {
                "width": capture.image_width,
                "height": capture.image_height,
            },
            "image_part_dimensions": {
                "width": capture.image_width,
                "height": capture.image_height,
            },
            "source_dimensions": (
                {
                    "width": capture.transform.source_width,
                    "height": capture.transform.source_height,
                }
                if capture.transform is not None else {}
            ),
            "mime": capture.mime,
            "crop_rectangle": (
                [round(value, 3) for value in capture.transform.source_box]
                if capture.transform is not None else []
            ),
            "source_frame_id": refinement_source_frame_id,
            "refine_count": store.refinement_count,
            "crop_chain": {
                "source_frame_id": refinement_source_frame_id,
                "crop_rectangle": (
                    [round(value, 3) for value in capture.transform.source_box]
                    if capture.transform is not None else []
                ),
                "refine_count": store.refinement_count,
            },
            "target_identity": {
                "application": scene.application_id,
                "surface_id": requested.ref_id,
                "window_surface_id": scene.window_ref.ref_id,
                "window_generation": scene.window_ref.window_generation,
                "structural_epoch": scene.structural_epoch,
            },
            "source_bounds": (
                scene.window_ref.bounds.to_safe_dict()
                if scene.window_ref.bounds is not None else {}
            ),
            "content_coverage": content_coverage.value,
            "evidence_semantics": safe["evidence_semantics"],
            "surface_selection": surface_selection.safe_dict(
                surface_candidates,
                application_bounds=scene.window_ref.bounds,
                captured_dimensions={
                    "width": capture.image_width,
                    "height": capture.image_height,
                },
            ),
            **(
                {"surface_lifecycle": surface_lifecycle}
                if surface_lifecycle is not None else {}
            ),
        },
        "diagnostics": {
            **trace.safe_dict(),
            "image_input_available": True,
            "image_input_sent": False,
            "image_part_count": request.image_part_count,
            "provider_id": provider,
            "model_id": model,
            "frame_id": capture.frame_id,
        },
    }
    return result


def _visual_action_receipt(
    frame,
    bbox: NormalizedBBox,
    *,
    target: str,
    expected_postcondition: str,
    dispatch_state: str,
    outcome: str,
    reason_code: str,
    fresh_frame=None,
    surface_ref: str = "",
    window_generation: str = "",
    native_window_id: str = "",
    dispatch_count: int = 0,
    actuation_lease: dict | None = None,
) -> dict:
    """Build one privacy-bounded proposal/dispatch receipt for ActionTruth."""

    normalized_bbox = bbox.to_dict()
    proposal_material = json.dumps(
        {
            "source_frame_id": frame.frame_id,
            "normalized_bbox": normalized_bbox,
            "semantic_target": str(target or "")[:500],
            "expected_postcondition": str(expected_postcondition or "")[:500],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": 1,
        "proposal_id": "vprop_" + hashlib.sha256(
            proposal_material.encode("utf-8", errors="replace")
        ).hexdigest()[:32],
        "source_frame_id": frame.frame_id,
        "fresh_frame_id": str(
            getattr(fresh_frame, "frame_id", "") or ""
        ),
        "surface_ref": str(surface_ref or frame.surface_ref),
        "surface_generation": str(
            window_generation or frame.surface_generation
            or frame.window_generation
        ),
        "window_generation": str(
            window_generation or frame.window_generation
        ),
        "native_window_id": str(native_window_id or ""),
        "application": str(frame.application_id or ""),
        "frame_dimensions": {
            "width": int(frame.image_width),
            "height": int(frame.image_height),
        },
        "normalized_bbox": normalized_bbox,
        "expected_postcondition": str(expected_postcondition or "")[:800],
        "coordinate_transform": (
            frame.transform.safe_dict() if frame.transform is not None else {}
        ),
        "dispatch_state": str(dispatch_state or "not_dispatched"),
        "dispatch_count": max(0, min(1, int(dispatch_count))),
        "outcome": str(outcome or "confirmed_failure_before_effect"),
        "reason_code": str(reason_code or "VISUAL_PREFLIGHT_REJECTED"),
        "actuator": "active_model_visual_bbox_local_mouse",
        "actuation_lease": {
            "state": (
                "actuation_leased"
                if isinstance(actuation_lease, dict)
                and float(actuation_lease.get("lease_expires_at") or 0.0) > 0.0
                else "unavailable"
            ),
            "acknowledged_at": round(float(
                (actuation_lease or {}).get("acknowledged_at") or 0.0
            ), 6),
            "expires_at": round(float(
                (actuation_lease or {}).get("lease_expires_at") or 0.0
            ), 6),
        },
    }


def vision_guided_desktop_click(
    target: str,
    dispatch_click,
    *,
    visual_frame_id: str = "",
    visual_bbox: dict | None = None,
    expected_postcondition: str = "",
    surface_ref: str = "",
    expected_application: str = "",
    expected_window_generation: str = "",
    expected_native_window_id: str = "",
    before_dispatch=None,
    prepare_dispatch=None,
) -> dict:
    """Dispatch one bbox selected by the active model from its exact frame."""

    provider, model = effective_model_image_route()
    if not provider or not model:
        return {
            "dispatched": False,
            "outcome": "inapplicable",
            "text": (
                "ERROR: the active model has no image-input capability and no "
                "alternate model is available."
            ),
            "reason_code": "IMAGE_INPUT_UNAVAILABLE",
        }
    frame_id = str(visual_frame_id or "")
    frames = _VISUAL_FRAMES.get() or {}
    context_frame = frames.get(frame_id)
    frame, lease_reason, actuation_lease = _leased_visual_actuation_frame(
        frame_id,
        provider=provider,
        model=model,
    )
    if frame is None:
        return {
            "dispatched": False,
            "outcome": "inapplicable",
            "text": (
                "ERROR: desktop_click needs a provider-acknowledged leased "
                "visual_frame_id and normalized visual_bbox from "
                "request_visual_observation; no click was sent."
            ),
            "reason_code": (
                lease_reason
                if context_frame is not None
                or lease_reason == "VISUAL_FRAME_LEASE_EXPIRED"
                else "VISUAL_FRAME_REQUIRED"
            ),
        }
    if frame.provider != provider or frame.model != model:
        return {
            "dispatched": False,
            "outcome": "inapplicable",
            "text": (
                "ERROR: the visual frame belongs to a different model route; "
                "no click was sent."
            ),
            "reason_code": "VISUAL_FRAME_MODEL_MISMATCH",
        }
    bound_native_window_id = str(
        expected_native_window_id
        or (
            frame.target_identity[4]
            if len(frame.target_identity) > 4 else ""
        )
        or ""
    )
    if before_dispatch is None or not callable(before_dispatch):
        return {
            "dispatched": False,
            "outcome": "inapplicable",
            "text": (
                "ERROR: an authoritative ActionTruth dispatch context is "
                "required; no click was sent."
            ),
            "reason_code": "ACTION_TRUTH_CONTEXT_REQUIRED",
        }
    try:
        bbox = NormalizedBBox(**{
            key: float((visual_bbox or {})[key])
            for key in ("left", "top", "right", "bottom")
        })
    except (KeyError, TypeError, ValueError, OverflowError):
        return {
            "dispatched": False,
            "outcome": "ambiguous",
            "text": (
                "ERROR: visual_bbox must be one finite normalized rectangle; "
                "no click was sent."
            ),
            "reason_code": "VISUAL_BBOX_INVALID",
        }

    def _reject_after_proposal(
        *, outcome: str, text: str, reason_code: str,
        fresh_frame=None, surface_ref_value: str = "",
        window_generation_value: str = "",
    ) -> dict:
        return {
            "dispatched": False,
            "outcome": outcome,
            "text": text,
            "reason_code": reason_code,
            "receipt": _visual_action_receipt(
                frame,
                bbox,
                target=target,
                expected_postcondition=expected_postcondition,
                dispatch_state="not_dispatched",
                outcome="confirmed_failure_before_effect",
                reason_code=reason_code,
                fresh_frame=fresh_frame,
                surface_ref=surface_ref_value,
                window_generation=window_generation_value,
                native_window_id=bound_native_window_id,
                dispatch_count=0,
                actuation_lease=actuation_lease,
            ),
        }

    if frame.transform is None:
        return _reject_after_proposal(
            outcome="capture_invalid",
            text="ERROR: the visual frame has no coordinate transform.",
            reason_code="VISUAL_TRANSFORM_UNAVAILABLE",
        )

    current, requested, resolution_reason = _observe_exact_bound_visual_scene(
        str(surface_ref or frame.surface_ref),
        target=target,
        expected_application=(
            expected_application or frame.application_id
        ),
        expected_window_generation=(
            expected_window_generation or frame.window_generation
        ),
        expected_native_window_id=bound_native_window_id,
    )
    if current is None or requested is None:
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the exact frame target changed before click; no click "
                "was sent."
            ),
            reason_code=resolution_reason or "SURFACE_STALE",
        )
    if (
        current.window_ref.window_generation != frame.window_generation
        or current.application_id != frame.application_id
        or current.structural_epoch != frame.structural_epoch
    ):
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the exact surface generation changed before click; "
                "no click was sent."
            ),
            reason_code="GENERATION_MISMATCH",
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )
    current_window_bounds = current.window_ref.bounds
    current_window_box = (
        (
            current_window_bounds.left,
            current_window_bounds.top,
            current_window_bounds.right,
            current_window_bounds.bottom,
        )
        if current_window_bounds is not None else ()
    )
    if frame.window_bounds and current_window_box != tuple(frame.window_bounds):
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the exact window geometry changed after the visual "
                "frame; re-observe before mouse dispatch."
            ),
            reason_code="SURFACE_STALE_GEOMETRY_CHANGED",
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )
    if not current.foreground:
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the exact visual target is not foreground for local "
                "mouse dispatch; no click was sent."
            ),
            reason_code="VISUAL_TARGET_NOT_FOREGROUND",
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )

    try:
        fresh_frame = _capture_bound_visual_surface(
            current,
            requested,
            provider=provider,
            model=model,
        )
    except _ProtectedWindowCapture:
        return _reject_after_proposal(
            outcome="capture_invalid",
            text=(
                "ERROR: the exact target became capture-protected before click; "
                "no click was sent."
            ),
            reason_code="CAPTURE_PROTECTED",
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )
    except Exception:
        return _reject_after_proposal(
            outcome="capture_invalid",
            text=(
                "ERROR: a fresh exact pixel preflight could not be completed; "
                "no click was sent."
            ),
            reason_code="VISUAL_PREFLIGHT_CAPTURE_FAILED",
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )
    leased_frame, lease_reason, current_lease = _leased_visual_actuation_frame(
        frame_id,
        provider=provider,
        model=model,
    )
    if leased_frame is None or not fresh_frame.is_fresh():
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the visual actuation lease or fresh pixel preflight "
                "expired; "
                "no click was sent."
            ),
            reason_code=(lease_reason or "SURFACE_STALE"),
            fresh_frame=fresh_frame,
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )
    if not visual_frame_pixels_unchanged(frame, fresh_frame, bbox):
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the exact target pixels changed after the model selected "
                "its bbox; re-observe before mouse dispatch."
            ),
            reason_code="SURFACE_STALE_PIXELS_CHANGED",
            fresh_frame=fresh_frame,
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )

    left, top, right, bottom = frame.transform.bbox_screen_pixels(bbox)
    point = ((left + right) // 2, (top + bottom) // 2)
    window_bounds = current_window_bounds
    if (
        window_bounds is None
        or not (
            window_bounds.left <= point[0] < window_bounds.right
            and window_bounds.top <= point[1] < window_bounds.bottom
        )
    ):
        return _reject_after_proposal(
            outcome="ambiguous",
            text=(
                "ERROR: the mapped visual target is outside the exact window; "
                "no click was sent."
            ),
            reason_code="VISUAL_POINT_OUTSIDE_TARGET",
            fresh_frame=fresh_frame,
            surface_ref_value=requested.ref_id,
            window_generation_value=current.window_ref.window_generation,
        )

    prepared_dispatch = None
    if callable(prepare_dispatch):
        prepared_dispatch = prepare_dispatch(*point)
        if (
            isinstance(prepared_dispatch, str)
            and prepared_dispatch.upper().startswith("ERROR")
        ):
            return _reject_after_proposal(
                outcome="inapplicable",
                text=prepared_dispatch,
                reason_code="MOUSE_BACKEND_NOT_READY",
                fresh_frame=fresh_frame,
                surface_ref_value=requested.ref_id,
                window_generation_value=current.window_ref.window_generation,
            )

    final_scene, final_requested, final_reason = (
        _observe_exact_bound_visual_scene(
            str(surface_ref or frame.surface_ref),
            target=target,
            expected_application=(expected_application or frame.application_id),
            expected_window_generation=(
                expected_window_generation or frame.window_generation
            ),
            expected_native_window_id=bound_native_window_id,
        )
    )
    final_bounds = (
        final_scene.window_ref.bounds
        if final_scene is not None else None
    )
    if (
        final_scene is None
        or final_requested is None
        or not final_scene.foreground
        or final_scene.application_id != frame.application_id
        or final_scene.window_ref.window_generation != frame.window_generation
        or final_bounds is None
        or not (
            final_bounds.left <= point[0] < final_bounds.right
            and final_bounds.top <= point[1] < final_bounds.bottom
        )
    ):
        return _reject_after_proposal(
            outcome="stale",
            text=(
                "ERROR: the exact target lost foreground/identity at the final "
                "mouse boundary; no click was sent."
            ),
            reason_code=(final_reason or "LAST_MOMENT_FOREGROUND_LOST"),
            fresh_frame=fresh_frame,
            surface_ref_value=(
                final_requested.ref_id if final_requested is not None else ""
            ),
            window_generation_value=(
                final_scene.window_ref.window_generation
                if final_scene is not None else ""
            ),
        )
    before_dispatch()
    try:
        dispatched = (
            dispatch_click(prepared_dispatch)
            if callable(prepare_dispatch) else
            dispatch_click(*point)
        )
    except Exception:
        dispatched = "ERROR: click dispatch raised"
    receipt = _visual_action_receipt(
        frame,
        bbox,
        target=target,
        expected_postcondition=expected_postcondition,
        dispatch_state="effect_unknown",
        outcome="outcome_unknown",
        reason_code="VISUAL_CLICK_RECONCILIATION_REQUIRED",
        fresh_frame=fresh_frame,
        surface_ref=requested.ref_id,
        window_generation=current.window_ref.window_generation,
        native_window_id=bound_native_window_id,
        dispatch_count=1,
        actuation_lease=current_lease,
    )
    receipt.update({
        "application": current.application_id,
        "available_read_only_observers": [
            "inspect_desktop_surfaces",
            "request_visual_observation",
        ],
    })
    if isinstance(dispatched, str) and dispatched.upper().startswith("ERROR"):
        receipt["reason_code"] = "VISUAL_CLICK_DISPATCH_OUTCOME_UNKNOWN"
        return {
            "dispatched": True,
            "outcome": "ambiguous",
            "text": (
                f"{dispatched} Effect is unknown after the single ActionTruth "
                "boundary; report the uncertainty truthfully."
            ),
            "reason_code": receipt["reason_code"],
            "receipt": receipt,
        }
    return {
        "dispatched": True,
        "outcome": "ambiguous",
        "text": (
            f'ERROR: clicked "{target}" once from '
            "the active model's exact visual frame. The application effect is "
            "unknown; you may inspect state or request another action."
        ),
        "reason_code": receipt["reason_code"],
        "receipt": receipt,
    }


def _find_runtime_control(root, runtime_id: tuple[int, ...]):
    frontier = [root]
    visited = 0
    while frontier and visited < _MAX_UIA_NODES:
        control = frontier.pop(0)
        visited += 1
        if _runtime_id(control) == runtime_id:
            return control
        try:
            frontier.extend(control.GetChildren() or ())
        except Exception:
            pass
    return None


def _pre_dispatch_failure(
    text: str,
    reason_code: str,
    *,
    failure_class: str = "mechanism_failed_continue",
    scene: DesktopScene | None = None,
    target: str = "",
) -> dict:
    """Build one canonical no-effect receipt and optional same-turn evidence."""

    receipt = {
        "schema_version": 1,
        "dispatch_state": "not_dispatched",
        "outcome": "confirmed_failure_before_effect",
        "reason_code": str(reason_code),
        "failure_class": str(failure_class),
    }
    result = {"text": str(text), "receipt": receipt}
    if scene is None:
        return result

    window_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id
    )
    receipt["application_identity"] = scene.application_id
    quality = scene.accessibility_quality(target)
    receipt["accessibility_quality"] = quality.value
    receipt["window_surface_ref"] = scene.window_ref.ref_id
    receipt["window_generation"] = scene.window_ref.window_generation
    receipt["native_window_id"] = str(
        window_native.native_window_id if window_native is not None else ""
    )
    receipt["structural_epoch"] = scene.structural_epoch
    receipt["available_read_only_observers"] = (
        ["inspect_desktop_surfaces", "request_visual_observation"]
        if effective_model_supports_vision() else
        ["inspect_desktop_surfaces"]
    )
    return result


def activate_desktop_surface(
    surface_ref: str,
    objective: str,
    *,
    observable_effect: dict | None = None,
    action_pattern: str = "",
    semantic_batch: list[dict] | None = None,
    _batch_internal: bool = False,
    _batch_application: str = "",
    _batch_window_generation: str = "",
    _batch_structural_epoch: int | None = None,
) -> dict:
    """Re-resolve an opaque ref and invoke/select it through UIA, never coordinates."""

    if sys.platform != "win32":
        return _pre_dispatch_failure(
            "ERROR: native semantic activation is unavailable on this platform.",
            "native_accessibility_unavailable",
        )

    if semantic_batch is not None and not _batch_internal:
        try:
            from osia.semantic_batch import (
                execute_semantic_batch,
                normalize_semantic_batch,
            )

            plan, reason_code = normalize_semantic_batch(
                surface_ref=surface_ref,
                objective=objective,
                action_pattern=action_pattern,
                observable_effect=observable_effect,
                semantic_batch=semantic_batch,
            )
        except Exception:
            plan, reason_code = {}, "semantic_batch_preflight_unavailable"
        if not plan:
            return _pre_dispatch_failure(
                "ERROR: semantic batch rejected before dispatch.",
                reason_code,
            )
        anchor_resolution = DESKTOP_SURFACE_REGISTRY.resolve(surface_ref)
        anchor = anchor_resolution.surface
        if not anchor_resolution.resolved or anchor is None:
            return _pre_dispatch_failure(
                "ERROR: stale semantic batch anchor.",
                "semantic_batch_stale_primitive",
            )

        def _dispatch_batch_item(item: dict) -> dict:
            item_result = activate_desktop_surface(
                str(item.get("surface_ref") or ""),
                str(item.get("objective") or ""),
                observable_effect=item.get("observable_effect"),
                action_pattern=str(item.get("action_pattern") or ""),
                _batch_internal=True,
                _batch_application=anchor.application_id,
                _batch_window_generation=anchor.window_generation,
                _batch_structural_epoch=anchor.structural_epoch,
            )
            try:
                from osia.tool_attempts import (
                    current_tool_attempt_id,
                    current_tool_attempt_store,
                )

                attempt_id = current_tool_attempt_id()
                store = current_tool_attempt_store()
                item_receipt = (
                    item_result.get("receipt")
                    if isinstance(item_result, dict) else None
                )
                if attempt_id and store is not None and isinstance(
                    item_receipt, dict
                ):
                    store.record_semantic_batch_primitive_receipt(
                        attempt_id,
                        semantic_batch_id=str(
                            plan.get("semantic_batch_id") or ""
                        ),
                        batch_index=int(item.get("index") or 0),
                        receipt=item_receipt,
                    )
            except Exception:
                # Dispatch lifecycle evidence remains fail-closed in the
                # canonical journal; the aggregate binder gets one final
                # chance to attach this exact typed receipt.
                pass
            return item_result

        return execute_semantic_batch(plan, _dispatch_batch_item)

    prior_resolution = DESKTOP_SURFACE_REGISTRY.resolve(surface_ref)
    prior_surface = prior_resolution.surface
    if prior_surface is None:
        return _pre_dispatch_failure(
            "ERROR: stale_reference; re-observe and re-resolve the semantic target.",
            "stale_semantic_reference",
        )
    selection = _select_desktop_target(prior_surface.application_id)
    scene = selection.target_scene
    if scene is None:
        return _pre_dispatch_failure(
            "ERROR: exact requested desktop surface is unavailable or ambiguous.",
            "semantic_target_surface_unavailable",
            failure_class="surface_unavailable",
        )
    if _batch_internal and (
        (_batch_application and scene.application_id != _batch_application)
        or (
            _batch_window_generation
            and scene.window_ref.window_generation
            != _batch_window_generation
        )
        or (
            _batch_structural_epoch is not None
            and scene.structural_epoch != int(_batch_structural_epoch)
        )
    ):
        return _pre_dispatch_failure(
            "ERROR: semantic batch target structure changed before dispatch.",
            "semantic_batch_structural_transition",
            scene=scene,
            target=str(objective or ""),
        )
    runtime_ids = {
        DESKTOP_SURFACE_REGISTRY.native_identity(item.ref_id).runtime_id
        for item in scene.surfaces
        if DESKTOP_SURFACE_REGISTRY.native_identity(item.ref_id) is not None
    }
    current = next(
        (item for item in scene.surfaces if item.ref_id == str(surface_ref)), None,
    )
    resolution = (
        prior_resolution
        if prior_resolution.status == "stale" else
        DESKTOP_SURFACE_REGISTRY.resolve(
            surface_ref,
            current_generation=scene.window_ref.window_generation,
            current_epoch=scene.structural_epoch,
            current_runtime_ids=runtime_ids,
            current_bounds=current.bounds if current is not None else None,
        )
    )
    reresolved_from = ""
    if not resolution.resolved or resolution.native is None:
        replacement = DESKTOP_SURFACE_REGISTRY.prove_equivalent(
            surface_ref,
            scene.surfaces,
            objective_label=str(objective or ""),
        )
        replacement_resolution = (
            DESKTOP_SURFACE_REGISTRY.resolve(
                replacement.ref_id,
                current_generation=scene.window_ref.window_generation,
                current_epoch=scene.structural_epoch,
                current_runtime_ids=runtime_ids,
                current_bounds=replacement.bounds,
            )
            if replacement is not None else None
        )
        if (
            replacement_resolution is not None
            and replacement_resolution.resolved
            and replacement_resolution.native is not None
        ):
            reresolved_from = str(surface_ref)
            surface_ref = replacement.ref_id
            resolution = replacement_resolution
        else:
            failure = _pre_dispatch_failure(
                "ERROR: stale_reference; re-observe and re-resolve the semantic target.",
                "stale_semantic_reference",
                scene=scene,
                target=str(objective or ""),
            )
            failure["receipt"]["stale_reason"] = resolution.reason
            return failure
    normalized_objective = " ".join(str(objective or "").lower().split())
    normalized_label = " ".join(
        str(resolution.surface.accessible_label or "").lower().split()
    )
    if (
        not normalized_objective
        or not normalized_label
        or normalized_objective != normalized_label
    ):
        failure = _pre_dispatch_failure(
            (
                "ERROR: stale_reference; the stated objective no longer exactly "
                "matches the resolved semantic surface."
            ),
            "stale_semantic_reference",
            scene=scene,
            target=str(objective or ""),
        )
        failure["receipt"]["stale_reason"] = "objective_surface_mismatch"
        return failure
    is_window_target = bool(
        resolution.surface.ref_id == scene.window_ref.ref_id
        and resolution.surface.surface_kind in {
            SurfaceKind.WINDOW, SurfaceKind.DIALOG,
        }
    )
    selected_pattern = next((
        pattern_name
        for pattern_name, _getter_name, _method_name
        in _UIA_ACTION_PATTERN_METHODS
        if pattern_name.casefold() == str(action_pattern or "").casefold()
    ), "")
    window_pattern = (
        is_window_target
        and str(action_pattern or "").casefold() == "window"
    )
    if action_pattern and not selected_pattern and not window_pattern:
        return _pre_dispatch_failure(
            "ERROR: requested UIA action pattern is not supported by OSIA.",
            "uia_action_pattern_unsupported",
            scene=scene,
            target=str(objective or ""),
        )
    if is_window_target:
        if scene.foreground:
            return _pre_dispatch_failure(
                "ERROR: exact target window is already foreground; re-observe it.",
                "target_already_foreground",
                scene=scene,
                target=str(objective or ""),
            )
        from osia.tools import desktop_control as dc

        journal_started, journal_returned = (
            _durable_primitive_journal_callbacks(
                application_identity=scene.application_id,
                surface_ref=surface_ref,
                native_window_id=resolution.native.native_window_id,
                window_generation=scene.window_ref.window_generation,
                runtime_identity=resolution.surface.runtime_signature,
            )
        )
        if callable(journal_started) and not journal_started(
            uia_pattern="Window",
        ):
            return _pre_dispatch_failure(
                "ERROR: durable primitive journal unavailable before focus dispatch.",
                "primitive_journal_unavailable",
                scene=scene,
                target=str(objective or ""),
            )
        dispatched_at = time.time()
        try:
            focus_status, _focus_detail = dc.focus_app_window((
                resolution.native.native_window_id,
                resolution.surface.accessible_label,
                resolution.native.pid,
            ))
        except Exception:
            focus_status = "outcome_unknown"
        if callable(journal_returned) and not journal_returned():
            focus_status = "outcome_unknown"
        if focus_status != "ready":
            return {
                "text": "ERROR: exact target-window focus is unverified after dispatch.",
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "effect_unknown",
                    "outcome": "outcome_unknown",
                    "reason_code": "desktop_target_focus_outcome_unknown",
                    "failure_class": "effect_reconciliation_required",
                    "surface_ref": surface_ref,
                    "primitive": {
                        "kind": "desktop_window_focus",
                        "mechanism": "exact_window_focus",
                        "dispatched_at": dispatched_at,
                        "before": _primitive_state_projection(
                            scene, resolution.surface,
                        ),
                    },
                },
            }
        verified_selection = _select_desktop_target(prior_surface.application_id)
        verified_scene = verified_selection.target_scene
        verified_native = (
            DESKTOP_SURFACE_REGISTRY.native_identity(
                verified_scene.window_ref.ref_id,
            )
            if verified_scene is not None else None
        )
        exact_focus_verified = bool(
            verified_selection.status == "exact_target"
            and verified_scene is not None
            and verified_scene.foreground
            and verified_scene.application_id == scene.application_id
            and verified_scene.window_ref.process_ref
            == scene.window_ref.process_ref
            and verified_scene.window_ref.window_generation
            == scene.window_ref.window_generation
            and verified_native is not None
            and str(verified_native.native_window_id)
            == str(resolution.native.native_window_id)
            and int(verified_native.pid or 0) == int(resolution.native.pid or 0)
        )
        if not exact_focus_verified:
            return {
                "text": (
                    "ERROR: target-window focus was dispatched but the exact "
                    "HWND/process/surface foreground postcondition was not verified."
                ),
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "effect_unknown",
                    "outcome": "outcome_unknown",
                    "reason_code": "desktop_target_focus_verification_failed",
                    "failure_class": "effect_reconciliation_required",
                    "surface_ref": surface_ref,
                    "primitive": {
                        "kind": "desktop_window_focus",
                        "mechanism": "exact_window_focus",
                        "dispatched_at": dispatched_at,
                        "before": _primitive_state_projection(
                            scene, resolution.surface,
                        ),
                    },
                },
            }
        scene_payload = verified_scene.safe_dict(target=str(objective or ""))
        scene_payload.update({
            "requested_application": verified_selection.requested_application,
            "target_resolution": verified_selection.status,
            "target_identity_strength": verified_selection.identity_strength,
            "target_identity_evidence": verified_selection.identity_evidence,
            "target_candidate_count": verified_selection.candidate_count,
            "target_foreground": True,
            "foreground_application": verified_scene.application_id,
            "foreground_window_surface_ref": verified_scene.window_ref.ref_id,
            "top_level_surfaces": list(verified_selection.top_level_surfaces),
            "requested_target_candidates": list(
                verified_selection.requested_candidates
            ),
            "resolved_target_candidates": list(
                verified_selection.resolved_candidates
            ),
            "target_conflict": verified_selection.target_conflict,
        })
        invalidate_visual_actuation_frames(
            surface_ref=verified_scene.window_ref.ref_id,
            window_generation=verified_scene.window_ref.window_generation,
        )
        return {
            "text": (
                "Exact desktop target-window activation and fresh foreground "
                "reinspection verified; pre-focus visual localization was invalidated."
            ),
            "scene": scene_payload,
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
                "reason_code": "desktop_target_focus_verified",
                "surface_ref": surface_ref,
                "mechanism": "exact_window_focus",
                "structural_epoch": scene.structural_epoch,
                "observed_at": verified_scene.observed_at,
                "postcondition": "application_foreground",
                "target_foreground": True,
                "window_generation": verified_scene.window_ref.window_generation,
                "native_window_id": str(verified_native.native_window_id),
                "pid": int(verified_native.pid or 0),
                "reresolved_from": reresolved_from,
                "primitive": {
                    "kind": "desktop_window_focus",
                    "mechanism": "exact_window_focus",
                    "dispatched_at": dispatched_at,
                    "before": _primitive_state_projection(
                        scene, resolution.surface,
                    ),
                    "after": _primitive_state_projection(
                        verified_scene, verified_scene.window_ref,
                    ),
                },
            },
        }
    actuation_contract = _surface_actuation_contract(
        resolution.surface,
        target_foreground=scene.foreground,
        target_minimized=scene.minimized,
    )
    if scene.minimized:
        failure = _pre_dispatch_failure(
            (
                "ERROR: exact target is minimized; restore its window_surface_ref "
                "and re-observe before control actuation."
            ),
            "target_restored_required",
            failure_class="surface_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
        failure["receipt"]["target_window_surface_ref"] = scene.window_ref.ref_id
        return failure
    if not scene.foreground and not actuation_contract.get("background_safe"):
        failure = _pre_dispatch_failure(
            (
                "ERROR: exact target is background; activate its window_surface_ref "
                "and re-observe focus before UIA actuation."
            ),
            "target_focus_verification_required",
            failure_class="surface_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
        failure["receipt"]["target_window_surface_ref"] = scene.window_ref.ref_id
        return failure
    if not resolution.native.runtime_id:
        return _pre_dispatch_failure(
            "ERROR: surface has no actionable accessibility runtime identity.",
            "uia_runtime_id_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
    from osia.tools import desktop_control as dc

    auto = dc._load_uiautomation()
    if auto is None:
        return _pre_dispatch_failure(
            "ERROR: native accessibility is unavailable.",
            "native_accessibility_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
    try:
        from osia.execution_strategy import normalize_desktop_observable_effect

        declared_effect = normalize_desktop_observable_effect(
            observable_effect,
        )
    except Exception:
        declared_effect = {}
    primitive_before = _primitive_state_projection(
        scene,
        resolution.surface,
        witness_surface_refs=(declared_effect.get("surface_ref") or "",),
    )
    dispatch_started = False
    dispatch_started_at = 0.0
    dispatch_worker_may_be_active = False
    attempted_mechanism = ""
    pre_dispatch_failure = None
    actionability = {
        "candidate_identity": {
            "surface_ref": resolution.surface.ref_id,
            "runtime_signature": resolution.surface.runtime_signature,
            "role": resolution.surface.role,
            "label": resolution.surface.accessible_label,
            "window_surface_ref": scene.window_ref.ref_id,
            "window_generation": scene.window_ref.window_generation,
            "structural_epoch": scene.structural_epoch,
        },
        "automation_id": "",
        "semantic_match": True,
        "observed_patterns": [
            item for item in resolution.surface.patterns
            if item in _BACKGROUND_SAFE_UIA_PATTERNS
        ],
        "fresh_supported_patterns": [],
        "selected_pattern": selected_pattern,
        "selected_pattern_available": False,
        "actionable_match": False,
        "candidate_applicable": False,
        "dispatch_preflight_result": "pending_fresh_reference_check",
        "failed_before_effect_reason": "",
        "stale_rebind_status": (
            "reresolved" if reresolved_from else "fresh_exact_reference"
        ),
        "retirement_state": "active",
    }
    invoke_preflight_reason = ""
    if declared_effect:
        effect_surface = next((
            item for item in scene.surfaces
            if item.ref_id == declared_effect["surface_ref"]
            and item.role in _PRIMITIVE_STATE_ROLES
        ), None)
        if (
            effect_surface is None
            or effect_surface.ref_id == resolution.surface.ref_id
            or _surface_text_matches(
                effect_surface.accessible_label,
                declared_effect["expected_text"],
            )
        ):
            invoke_preflight_reason = (
                "observable_effect_precondition_unverified"
            )
    try:
        journal_started, journal_returned = (
            _durable_primitive_journal_callbacks(
                application_identity=scene.application_id,
                surface_ref=surface_ref,
                native_window_id=resolution.native.native_window_id,
                window_generation=scene.window_ref.window_generation,
                runtime_identity=resolution.surface.runtime_signature,
            )
        )
        dispatch = _dispatch_fresh_uia_action_bounded(
            auto,
            native_window_id=resolution.native.native_window_id,
            runtime_id=resolution.native.runtime_id,
            action_pattern=selected_pattern,
            observed_patterns=resolution.surface.patterns,
            invoke_preflight_reason=invoke_preflight_reason,
            journal_dispatch_started=journal_started,
            journal_dispatch_returned=journal_returned,
        )
        selected_pattern = str(dispatch.get("selected_pattern") or "")
        actionability.update({
            "automation_id": str(dispatch.get("automation_id") or ""),
            "fresh_supported_patterns": list(
                dispatch.get("fresh_supported_patterns") or []
            ),
            "selected_pattern": selected_pattern,
            "selected_pattern_available": bool(
                dispatch.get("selected_pattern_available")
            ),
            "actionable_match": bool(
                dispatch.get("selected_pattern_available")
            ),
            "candidate_applicable": bool(
                dispatch.get("selected_pattern_available")
            ),
        })
        attempted_mechanism = selected_pattern.casefold()
        dispatch_started_at = float(dispatch.get("dispatched_at") or 0.0)
        dispatch_started = bool(dispatch.get("dispatch_started"))
        dispatch_worker_may_be_active = bool(
            dispatch.get("worker_may_be_active")
        )
        if not dispatch_started:
            reason_code = str(dispatch.get("reason_code") or (
                "native_accessibility_invocation_unavailable"
            ))
            if reason_code == "stale_semantic_reference":
                DESKTOP_SURFACE_REGISTRY.mark_stale(
                    surface_ref, "accessibility_runtime_id_disappeared",
                )
                failure_text = (
                    "ERROR: stale_reference; accessibility target disappeared."
                )
                stale_reason = "accessibility_runtime_id_disappeared"
                actionability["stale_rebind_status"] = (
                    "fresh_runtime_reference_missing"
                )
            elif reason_code == "observable_effect_precondition_unverified":
                failure_text = (
                    "ERROR: declared observable effect is not a fresh exact "
                    "surface transition."
                )
                stale_reason = ""
            elif reason_code == "uia_action_pattern_unavailable":
                failure_text = (
                    "ERROR: accessibility control has no safe actionable pattern."
                )
                stale_reason = ""
            else:
                failure_text = (
                    "ERROR: native accessibility invocation was unavailable "
                    "before dispatch."
                )
                stale_reason = ""
            pre_dispatch_failure = (
                failure_text,
                reason_code,
                stale_reason,
            )
            actionability.update({
                "dispatch_preflight_result": "failed_before_effect",
                "failed_before_effect_reason": reason_code,
                "retirement_state": "eligible_for_exact_pair_retirement",
            })
            mechanism = ""
        elif not dispatch.get("completed"):
            raise RuntimeError(
                str(dispatch.get("reason_code") or (
                    "uia_activation_outcome_unknown"
                ))
            )
        else:
            mechanism = attempted_mechanism
    except Exception:
        if not dispatch_started:
            return _pre_dispatch_failure(
                "ERROR: native accessibility invocation was unavailable before dispatch.",
                "native_accessibility_invocation_unavailable",
                scene=scene,
                target=str(objective or ""),
            )
        return {
            "text": "ERROR: native semantic activation failed after possible effect.",
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_unknown",
                "outcome": "outcome_unknown",
                "reason_code": "uia_activation_outcome_unknown",
                "failure_class": "effect_reconciliation_required",
                "surface_ref": surface_ref,
                "structural_epoch": scene.structural_epoch,
                "reresolved_from": reresolved_from,
                "actionability": {
                    **actionability,
                    "dispatch_preflight_result": "effect_unknown",
                    "retirement_state": "reconciliation_required",
                },
                "primitive": {
                    "kind": "desktop_uia_primitive",
                    "mechanism": attempted_mechanism,
                    "observable_effect": dict(declared_effect),
                    "dispatched_at": dispatch_started_at,
                    "worker_may_be_active": (
                        dispatch_worker_may_be_active
                    ),
                    "before": primitive_before,
                },
            },
        }
    if pre_dispatch_failure is not None:
        text, reason_code, stale_reason = pre_dispatch_failure
        failure = _pre_dispatch_failure(
            text,
            reason_code,
            scene=scene,
            target=str(objective or ""),
        )
        if stale_reason:
            failure["receipt"]["stale_reason"] = stale_reason
        failure["receipt"].update({
            "surface_ref": surface_ref,
            "runtime_identity": resolution.surface.runtime_signature,
            "automation_id": str(actionability.get("automation_id") or ""),
            "uia_pattern": str(
                actionability.get("selected_pattern") or selected_pattern or ""
            ),
        })
        failure["receipt"]["actionability"] = actionability
        return failure
    actionability["dispatch_preflight_result"] = "dispatched"
    actionability["retirement_state"] = "active_pending_verification"
    return {
        "text": "Desktop surface activated through native accessibility.",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "dispatched",
            "outcome": "success_unverified",
            "reason_code": "native_accessibility_dispatched",
            "surface_ref": surface_ref,
            "mechanism": mechanism,
            "actionability": actionability,
            "actuation_preconditions": list(
                actuation_contract.get("preconditions") or []
            ),
            "background_safe": bool(
                actuation_contract.get("background_safe")
            ),
            "structural_epoch": scene.structural_epoch,
            "reresolved_from": reresolved_from,
            "primitive": {
                "kind": "desktop_uia_primitive",
                "mechanism": mechanism,
                "observable_effect": dict(declared_effect),
                "dispatched_at": dispatch_started_at,
                "worker_may_be_active": dispatch_worker_may_be_active,
                "before": primitive_before,
            },
        },
    }


__all__ = [
    "DesktopScene",
    "NativeWindowSurfaceCandidate",
    "SurfaceCoverage",
    "VisualSurfaceSelection",
    "activate_desktop_surface",
    "begin_desktop_turn",
    "bind_effective_model",
    "effective_model_supports_vision",
    "effective_model_route_is_bound",
    "effective_model_image_route",
    "effective_model_media_scope",
    "end_desktop_turn",
    "focus_existing_application_window",
    "inspect_desktop_surfaces",
    "observe_foreground_scene",
    "rank_surface_candidates",
    "request_visual_observation",
    "reset_effective_model",
    "scene_matches_application",
    "select_application_visual_surface",
    "vision_guided_desktop_click",
]
