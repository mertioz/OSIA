"""Bounded, ephemeral image observations correlated to desktop surfaces."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class CaptureKind(str, Enum):
    WINDOW = "window"
    REGION = "region"
    CROP = "crop"
    ZOOM_REFINE = "zoom_refine"
    GLOBAL = "global"


class ObservationReason(str, Enum):
    AMBIGUITY = "ambiguity"
    INCOMPLETE_ACCESSIBILITY = "incomplete_accessibility"
    CUSTOM_RENDERED = "custom_rendered"
    POPUP_OR_OVERLAY = "popup_or_overlay"
    SMALL_TARGET = "small_target"
    STATE_CONFLICT = "state_conflict"
    VERIFICATION = "verification"


@dataclass(frozen=True)
class VisualObservation:
    observation_id: str
    surface_ref: str
    capture_kind: CaptureKind
    reason: ObservationReason
    width: int
    height: int
    capture_epoch: int
    structural_epoch: int
    surface_fingerprint: str
    image_sha256: str
    observed_at: float
    expires_at: float
    deduplicated: bool = False
    stale: bool = False

    def to_safe_dict(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "surface_ref": self.surface_ref,
            "capture_kind": self.capture_kind.value,
            "reason": self.reason.value,
            "dimensions": {"width": self.width, "height": self.height},
            "capture_epoch": self.capture_epoch,
            "structural_epoch": self.structural_epoch,
            "surface_fingerprint": self.surface_fingerprint,
            "image_sha256": self.image_sha256,
            "observed_at": round(self.observed_at, 3),
            "expires_at": round(self.expires_at, 3),
            "deduplicated": self.deduplicated,
            "stale": self.stale or time.time() >= self.expires_at,
        }


@dataclass
class _StoredObservation:
    public: VisualObservation
    png_bytes: bytes = field(repr=False)


class VisualObservationStore:
    """One-turn image cache with capture budgets and hash deduplication."""

    def __init__(
        self,
        *,
        maximum_captures: int = 4,
        maximum_global_captures: int = 1,
        maximum_refinements: int = 1,
        ttl_seconds: float = 90.0,
    ) -> None:
        self.maximum_captures = max(1, int(maximum_captures))
        self.maximum_global_captures = max(0, int(maximum_global_captures))
        self.maximum_refinements = max(0, int(maximum_refinements))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._observations: dict[str, _StoredObservation] = {}
        self._dedup: dict[tuple[str, int, str], str] = {}
        self._capture_count = 0
        self._global_count = 0
        self._refine_count = 0
        self._stagnation_count = 0
        self._lock = threading.RLock()

    @property
    def capture_count(self) -> int:
        return self._capture_count

    @property
    def stagnation_count(self) -> int:
        return self._stagnation_count

    def budget_allows(self, capture_kind: CaptureKind) -> bool:
        with self._lock:
            if self._capture_count >= self.maximum_captures:
                return False
            if (
                capture_kind is CaptureKind.GLOBAL
                and self._global_count >= self.maximum_global_captures
            ):
                return False
            if (
                capture_kind is CaptureKind.ZOOM_REFINE
                and self._refine_count >= self.maximum_refinements
            ):
                return False
            return True

    def add(
        self,
        *,
        surface_ref: str,
        capture_kind: CaptureKind,
        reason: ObservationReason,
        width: int,
        height: int,
        capture_epoch: int,
        structural_epoch: int,
        surface_fingerprint: str,
        png_bytes: bytes,
    ) -> tuple[VisualObservation, bool]:
        if not png_bytes:
            raise ValueError("empty_visual_observation")
        image_hash = hashlib.sha256(png_bytes).hexdigest()
        dedup_key = (str(surface_ref), int(structural_epoch), image_hash)
        now = time.time()
        with self._lock:
            prior_id = self._dedup.get(dedup_key)
            prior = self._observations.get(prior_id or "")
            if prior is not None and now < prior.public.expires_at:
                self._stagnation_count += 1
                duplicate = VisualObservation(
                    **{
                        **prior.public.__dict__,
                        "deduplicated": True,
                    }
                )
                return duplicate, False
            if not self.budget_allows(capture_kind):
                raise RuntimeError("visual_observation_budget_exhausted")
            observation = VisualObservation(
                observation_id="vobs_" + secrets.token_urlsafe(18),
                surface_ref=str(surface_ref),
                capture_kind=capture_kind,
                reason=reason,
                width=max(1, int(width)),
                height=max(1, int(height)),
                capture_epoch=max(0, int(capture_epoch)),
                structural_epoch=max(0, int(structural_epoch)),
                surface_fingerprint=str(surface_fingerprint)[:96],
                image_sha256=image_hash,
                observed_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._observations[observation.observation_id] = _StoredObservation(
                observation,
                bytes(png_bytes),
            )
            self._dedup[dedup_key] = observation.observation_id
            self._capture_count += 1
            if capture_kind is CaptureKind.GLOBAL:
                self._global_count += 1
            if capture_kind is CaptureKind.ZOOM_REFINE:
                self._refine_count += 1
            return observation, True

    def image_base64(self, observation_id: str) -> str | None:
        with self._lock:
            stored = self._observations.get(str(observation_id or ""))
            if stored is None or time.time() >= stored.public.expires_at:
                return None
            return base64.b64encode(stored.png_bytes).decode("ascii")

    def current(
        self,
        surface_ref: str,
        structural_epoch: int,
    ) -> VisualObservation | None:
        """Return the newest unexpired observation for the unchanged surface."""

        now = time.time()
        with self._lock:
            matches = [
                stored.public
                for stored in self._observations.values()
                if stored.public.surface_ref == str(surface_ref)
                and stored.public.structural_epoch == int(structural_epoch)
                and not stored.public.stale
                and now < stored.public.expires_at
            ]
        return max(matches, key=lambda item: item.observed_at) if matches else None

    def mark_scene_stale(self, surface_ref: str, structural_epoch: int) -> None:
        with self._lock:
            for key, stored in tuple(self._observations.items()):
                public = stored.public
                if (
                    public.surface_ref == str(surface_ref)
                    and public.structural_epoch != int(structural_epoch)
                    and not public.stale
                ):
                    stored.public = VisualObservation(
                        **{**public.__dict__, "stale": True}
                    )

    def clear(self) -> None:
        """Drop every image byte at the end of the turn."""

        with self._lock:
            self._observations.clear()
            self._dedup.clear()


__all__ = [
    "CaptureKind",
    "ObservationReason",
    "VisualObservation",
    "VisualObservationStore",
]
