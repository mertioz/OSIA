"""Opaque, stale-aware semantic references for native desktop surfaces.

The public reference is intentionally free of HWND, PID and UIA RuntimeId values.
Those native identifiers stay in the private registry and are used only to prove
that a reference still denotes the same process/window/control generation.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, Mapping


class SurfaceKind(str, Enum):
    WINDOW = "window"
    DIALOG = "dialog"
    TAB = "tab"
    PANE = "pane"
    CONVERSATION = "conversation"
    LIST = "list"
    TOOLBAR = "toolbar"
    CONTROL = "control"
    CUSTOM_REGION = "custom_region"


@dataclass(frozen=True)
class SurfaceBounds:
    """Signed physical-screen bounds plus optional window-relative bounds."""

    left: int
    top: int
    right: int
    bottom: int
    relative_left: int | None = None
    relative_top: int | None = None
    relative_right: int | None = None
    relative_bottom: int | None = None
    dpi_scale: float = 1.0

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return (
            self.left + self.width // 2,
            self.top + self.height // 2,
        )

    def valid(self) -> bool:
        return (
            self.width > 0
            and self.height > 0
            and self.left > -100_000
            and self.top > -100_000
            and self.right < 100_000
            and self.bottom < 100_000
        )

    def close_to(self, other: "SurfaceBounds", tolerance: int = 6) -> bool:
        return all(
            abs(first - second) <= max(0, int(tolerance))
            for first, second in zip(
                (self.left, self.top, self.right, self.bottom),
                (other.left, other.top, other.right, other.bottom),
            )
        )

    def to_safe_dict(self) -> dict:
        payload = {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
            "dpi_scale": round(float(self.dpi_scale), 3),
        }
        if self.relative_left is not None:
            payload["window_relative"] = {
                "left": self.relative_left,
                "top": self.relative_top,
                "right": self.relative_right,
                "bottom": self.relative_bottom,
            }
        return payload


@dataclass(frozen=True)
class DesktopSurfaceRef:
    ref_id: str
    application_id: str
    process_ref: str
    window_generation: str
    surface_kind: SurfaceKind
    role: str
    accessible_label: str
    selected: bool = False
    focused: bool = False
    enabled: bool = True
    parent_ref: str | None = None
    bounds: SurfaceBounds | None = None
    runtime_signature: str = ""
    patterns: tuple[str, ...] = ()
    structural_epoch: int = 0
    observed_at: float = field(default_factory=time.time)
    automation_id: str = ""

    def to_safe_dict(self) -> dict:
        payload = {
            "surface_ref": self.ref_id,
            "application": self.application_id,
            "process_ref": self.process_ref,
            "window_generation": self.window_generation,
            "surface_kind": self.surface_kind.value,
            "role": self.role,
            "label": self.accessible_label,
            "selected": self.selected,
            "focused": self.focused,
            "enabled": self.enabled,
            "parent_surface_ref": self.parent_ref,
            "structural_epoch": self.structural_epoch,
            "observed_at": round(self.observed_at, 3),
        }
        if self.bounds is not None:
            payload["bounds"] = self.bounds.to_safe_dict()
        if self.runtime_signature:
            payload["runtime_signature"] = self.runtime_signature
        if self.automation_id:
            payload["automation_id"] = self.automation_id
        if self.patterns:
            payload["patterns"] = list(self.patterns)
        return payload


@dataclass(frozen=True, repr=False)
class NativeSurfaceIdentity:
    """Private re-resolution authority. Never serialize this object."""

    pid: int
    process_started_at: float
    native_window_id: object
    runtime_id: tuple[int, ...] = ()
    ancestor_signature: str = ""
    class_name: str = ""


@dataclass
class _RegistryRecord:
    public: DesktopSurfaceRef
    native: NativeSurfaceIdentity
    stable_key: str
    semantic_identity: str
    scene_signature: str
    stale_reason: str = ""


@dataclass(frozen=True)
class SurfaceResolution:
    status: str
    surface: DesktopSurfaceRef | None
    native: NativeSurfaceIdentity | None
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == "resolved" and self.surface is not None


def _digest(*values: object, size: int = 20) -> str:
    material = "\x1f".join(str(value or "") for value in values)
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:size]


def process_reference(application_id: str, pid: int, started_at: float) -> str:
    return "proc_" + _digest(application_id, int(pid), f"{started_at:.6f}", size=24)


def window_generation(
    process_ref: str,
    native_window_id: object,
    class_name: str,
) -> str:
    return "win_" + _digest(process_ref, native_window_id, class_name, size=24)


class DesktopSurfaceRegistry:
    """Turn-independent opaque references with conservative stale semantics."""

    def __init__(self) -> None:
        self._records: dict[str, _RegistryRecord] = {}
        self._stable_refs: dict[str, str] = {}
        self._window_scene_refs: dict[str, set[str]] = {}
        self._window_scene_signatures: dict[str, str] = {}
        self._window_epochs: dict[str, int] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _new_ref() -> str:
        return "dsr_" + secrets.token_urlsafe(18)

    def observe_window_scene(
        self,
        *,
        application_id: str,
        pid: int,
        process_started_at: float,
        native_window_id: object,
        class_name: str,
        scene_signature: str,
    ) -> tuple[str, str, int]:
        proc_ref = process_reference(application_id, pid, process_started_at)
        generation = window_generation(proc_ref, native_window_id, class_name)
        with self._lock:
            previous = self._window_scene_signatures.get(generation)
            epoch = self._window_epochs.get(generation, 0)
            if previous is not None and previous != scene_signature:
                epoch += 1
                for ref_id in self._window_scene_refs.get(generation, set()):
                    record = self._records.get(ref_id)
                    if record is not None and not record.stale_reason:
                        record.stale_reason = "structural_epoch_changed"
                self._window_scene_refs[generation] = set()
                stale_keys = [
                    key for key, ref_id in self._stable_refs.items()
                    if ref_id in self._records
                    and self._records[ref_id].public.window_generation == generation
                ]
                for key in stale_keys:
                    self._stable_refs.pop(key, None)
            self._window_scene_signatures[generation] = scene_signature
            self._window_epochs[generation] = epoch
        return proc_ref, generation, epoch

    def register(
        self,
        *,
        application_id: str,
        process_ref: str,
        generation: str,
        surface_kind: SurfaceKind,
        role: str,
        accessible_label: str,
        selected: bool,
        focused: bool,
        enabled: bool,
        parent_ref: str | None,
        bounds: SurfaceBounds | None,
        runtime_signature: str,
        structural_epoch: int,
        scene_signature: str,
        native: NativeSurfaceIdentity,
        stable_identity: str,
        patterns: Iterable[str] = (),
        automation_id: str = "",
    ) -> DesktopSurfaceRef:
        stable_parent = "" if runtime_signature else parent_ref
        stable_key = _digest(
            generation,
            structural_epoch,
            stable_identity,
            role,
            stable_parent,
            size=40,
        )
        with self._lock:
            ref_id = self._stable_refs.get(stable_key)
            existing = self._records.get(ref_id or "")
            if existing is not None and not existing.stale_reason:
                bounds_changed = bool(
                    existing.public.bounds is not None
                    and bounds is not None
                    and not existing.public.bounds.close_to(bounds)
                )
                bounds_appeared_or_disappeared = (
                    (existing.public.bounds is None) != (bounds is None)
                )
                if bounds_changed or bounds_appeared_or_disappeared:
                    existing.stale_reason = "surface_bounds_changed"
                    self._stable_refs.pop(stable_key, None)
                else:
                    public = replace(
                        existing.public,
                        accessible_label=accessible_label,
                        selected=bool(selected),
                        focused=bool(focused),
                        enabled=bool(enabled),
                        parent_ref=parent_ref,
                        bounds=bounds,
                        automation_id=str(automation_id or "")[:160],
                        patterns=tuple(str(item) for item in patterns)[:16],
                        observed_at=time.time(),
                    )
                    existing.public = public
                    existing.native = native
                    existing.scene_signature = scene_signature
                    return public

            ref_id = self._new_ref()
            public = DesktopSurfaceRef(
                ref_id=ref_id,
                application_id=application_id,
                process_ref=process_ref,
                window_generation=generation,
                surface_kind=surface_kind,
                role=role,
                accessible_label=accessible_label,
                selected=bool(selected),
                focused=bool(focused),
                enabled=bool(enabled),
                parent_ref=parent_ref,
                bounds=bounds,
                runtime_signature=runtime_signature,
                automation_id=str(automation_id or "")[:160],
                patterns=tuple(str(item) for item in patterns)[:16],
                structural_epoch=int(structural_epoch),
            )
            self._records[ref_id] = _RegistryRecord(
                public=public,
                native=native,
                stable_key=stable_key,
                semantic_identity=str(stable_identity or ""),
                scene_signature=scene_signature,
            )
            self._stable_refs[stable_key] = ref_id
            self._window_scene_refs.setdefault(generation, set()).add(ref_id)
            return public

    def resolve(
        self,
        ref_id: str,
        *,
        current_generation: str | None = None,
        current_epoch: int | None = None,
        current_runtime_ids: Iterable[tuple[int, ...]] | None = None,
        current_bounds: SurfaceBounds | None = None,
        bounds_tolerance: int = 6,
    ) -> SurfaceResolution:
        with self._lock:
            record = self._records.get(str(ref_id or ""))
            if record is None:
                return SurfaceResolution("unknown", None, None, "unknown_surface_ref")
            if record.stale_reason:
                return SurfaceResolution(
                    "stale", record.public, None, record.stale_reason,
                )
            public = record.public
            if current_generation is not None and public.window_generation != current_generation:
                record.stale_reason = "window_generation_changed"
            elif current_epoch is not None and public.structural_epoch != int(current_epoch):
                record.stale_reason = "structural_epoch_changed"
            elif (
                current_runtime_ids is not None
                and record.native.runtime_id
                and record.native.runtime_id not in set(current_runtime_ids)
            ):
                record.stale_reason = "accessibility_runtime_id_disappeared"
            elif (
                current_bounds is not None
                and public.bounds is not None
                and not public.bounds.close_to(current_bounds, bounds_tolerance)
            ):
                record.stale_reason = "surface_bounds_changed"
            if record.stale_reason:
                return SurfaceResolution(
                    "stale", public, None, record.stale_reason,
                )
            return SurfaceResolution("resolved", public, record.native)

    def mark_stale(self, ref_id: str, reason: str) -> None:
        with self._lock:
            record = self._records.get(str(ref_id or ""))
            if record is not None and not record.stale_reason:
                record.stale_reason = str(reason or "stale_reference")[:80]

    def native_identity(self, ref_id: str) -> NativeSurfaceIdentity | None:
        resolved = self.resolve(ref_id)
        return resolved.native if resolved.resolved else None

    def refresh_anchor(self, ref_id: str) -> SurfaceResolution:
        """Return native authority only for a current or safely refreshable ref.

        Structural and bounds changes can obsolete a public snapshot without
        proving that its native target was replaced.  Callers may use this
        private identity only to re-observe the exact HWND and must then call
        :meth:`rebind_same_target` before using the refreshed public surface.
        Other stale reasons remain opaque and fail closed.
        """

        with self._lock:
            record = self._records.get(str(ref_id or ""))
            if record is None:
                return SurfaceResolution(
                    "unknown", None, None, "unknown_surface_ref",
                )
            if record.stale_reason not in {
                "", "structural_epoch_changed", "surface_bounds_changed",
            }:
                return SurfaceResolution(
                    "stale", record.public, None, record.stale_reason,
                )
            return SurfaceResolution(
                (
                    "resolved"
                    if not record.stale_reason else
                    "refreshable"
                ),
                record.public,
                record.native,
                record.stale_reason,
            )

    def rebind_same_target(
        self,
        ref_id: str,
        candidates: Iterable[DesktopSurfaceRef],
    ) -> SurfaceResolution:
        """Bind an old snapshot to one unique current same-native surface.

        Geometry and scene epoch are deliberately not identity fields here.
        Native window identity, process lifetime, generation and semantic
        identity all remain exact fences, so a destroyed/recycled/replaced
        target cannot inherit the old reference.
        """

        with self._lock:
            old_record = self._records.get(str(ref_id or ""))
            if old_record is None:
                return SurfaceResolution(
                    "unknown", None, None, "unknown_surface_ref",
                )
            old_public = old_record.public
            old_native = old_record.native
            matches: list[_RegistryRecord] = []
            same_window_seen = False
            for candidate in candidates:
                candidate_record = self._records.get(candidate.ref_id)
                if candidate_record is None or candidate_record.stale_reason:
                    continue
                current = candidate_record.public
                native = candidate_record.native
                same_native_window = bool(
                    int(native.pid) == int(old_native.pid)
                    and abs(
                        float(native.process_started_at)
                        - float(old_native.process_started_at)
                    ) <= 0.05
                    and native.native_window_id == old_native.native_window_id
                    and native.class_name == old_native.class_name
                )
                if not same_native_window:
                    continue
                same_window_seen = True
                if (
                    current.application_id == old_public.application_id
                    and current.process_ref == old_public.process_ref
                    and current.window_generation
                    == old_public.window_generation
                    and current.surface_kind is old_public.surface_kind
                    and current.role == old_public.role
                    and candidate_record.semantic_identity
                    == old_record.semantic_identity
                    and native.runtime_id == old_native.runtime_id
                    and native.ancestor_signature
                    == old_native.ancestor_signature
                ):
                    matches.append(candidate_record)
            if len(matches) == 1:
                match = matches[0]
                return SurfaceResolution(
                    "resolved", match.public, match.native,
                    "same_target_refreshed",
                )
            return SurfaceResolution(
                "stale",
                old_public,
                None,
                (
                    "semantic_target_changed"
                    if same_window_seen else
                    "native_target_changed"
                ),
            )

    def safe_snapshot(self, ref_ids: Iterable[str]) -> list[dict]:
        out = []
        with self._lock:
            for ref_id in ref_ids:
                record = self._records.get(str(ref_id or ""))
                if record is None:
                    continue
                item = record.public.to_safe_dict()
                item["stale"] = bool(record.stale_reason)
                if record.stale_reason:
                    item["stale_reason"] = record.stale_reason
                out.append(item)
        return out

    def prove_equivalent(
        self,
        stale_ref: str,
        candidates: Iterable[DesktopSurfaceRef],
        *,
        objective_label: str = "",
    ) -> DesktopSurfaceRef | None:
        """Return one unique same-role/label replacement, otherwise fail closed."""

        with self._lock:
            record = self._records.get(str(stale_ref or ""))
            if record is None:
                return None
            old = record.public
        expected = " ".join((objective_label or old.accessible_label).lower().split())
        matches = []
        for candidate in candidates:
            label = " ".join(candidate.accessible_label.lower().split())
            if (
                candidate.ref_id != old.ref_id
                and candidate.application_id == old.application_id
                and candidate.process_ref == old.process_ref
                and candidate.surface_kind is old.surface_kind
                and candidate.role == old.role
                and candidate.enabled
                and label
                and label == expected
            ):
                matches.append(candidate)
        return matches[0] if len(matches) == 1 else None


DESKTOP_SURFACE_REGISTRY = DesktopSurfaceRegistry()


__all__ = [
    "DESKTOP_SURFACE_REGISTRY",
    "DesktopSurfaceRef",
    "DesktopSurfaceRegistry",
    "NativeSurfaceIdentity",
    "SurfaceBounds",
    "SurfaceKind",
    "SurfaceResolution",
    "process_reference",
    "window_generation",
]
