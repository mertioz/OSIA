"""Claim-level semantics for partial, target-bound observer evidence.

The model may choose any applicable observer.  This module does not impose an
observer order; it only prevents an omission from an incomplete source from
becoming a verified negative claim.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import math
from typing import Any, Iterable


class ClaimStatus(str, Enum):
    PRESENT = "present"
    ABSENT_VERIFIED = "absent_verified"
    UNKNOWN = "unknown"
    CONFLICTING = "conflicting"


class EvidenceSource(str, Enum):
    API = "api"
    DOM = "dom"
    UIA = "uia"
    VISION = "vision"
    PROVIDER_RECEIPT = "provider_receipt"
    APPLICATION_STATE = "application_state"
    OTHER = "other"


class EvidenceCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class EvidenceFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


def _enum_value(value, enum_type, default):
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value or "").strip().lower())
    except ValueError:
        return default


def _safe_identifier(value: object, maximum: int = 180) -> str:
    return str(value or "").strip()[:maximum]


@dataclass(frozen=True)
class ClaimEvidence:
    claim: str
    value: Any = None
    status: ClaimStatus = ClaimStatus.UNKNOWN
    source: EvidenceSource = EvidenceSource.OTHER
    coverage: EvidenceCoverage = EvidenceCoverage.UNKNOWN
    confidence: float = 0.0
    freshness: EvidenceFreshness = EvidenceFreshness.UNKNOWN
    target_identity: str = ""
    frame_id: str = ""
    surface_id: str = ""
    observed_at: float = 0.0
    absence_proven: bool = False
    conflict: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status",
            _enum_value(self.status, ClaimStatus, ClaimStatus.UNKNOWN),
        )
        object.__setattr__(
            self, "source",
            _enum_value(self.source, EvidenceSource, EvidenceSource.OTHER),
        )
        object.__setattr__(
            self, "coverage",
            _enum_value(self.coverage, EvidenceCoverage, EvidenceCoverage.UNKNOWN),
        )
        object.__setattr__(
            self, "freshness",
            _enum_value(self.freshness, EvidenceFreshness, EvidenceFreshness.UNKNOWN),
        )
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        object.__setattr__(self, "confidence", min(1.0, max(0.0, confidence)))
        object.__setattr__(self, "claim", _safe_identifier(self.claim, 500))
        object.__setattr__(
            self, "target_identity", _safe_identifier(self.target_identity, 300),
        )
        object.__setattr__(self, "frame_id", _safe_identifier(self.frame_id))
        object.__setattr__(self, "surface_id", _safe_identifier(self.surface_id))
        if self.status is ClaimStatus.ABSENT_VERIFIED and not (
            self.absence_proven
            and self.coverage is EvidenceCoverage.COMPLETE
            and self.freshness is EvidenceFreshness.FRESH
            and self.target_identity
        ):
            object.__setattr__(self, "status", ClaimStatus.UNKNOWN)
            object.__setattr__(self, "absence_proven", False)

    def safe_dict(self) -> dict:
        """Return privacy-safe metadata without the observed value/content."""

        return {
            "schema_version": 1,
            "claim_ref": "claim_" + hashlib.sha256(
                self.claim.encode("utf-8", errors="replace")
            ).hexdigest()[:20],
            "status": self.status.value,
            "source": self.source.value,
            "coverage": self.coverage.value,
            "confidence": round(self.confidence, 6),
            "freshness": self.freshness.value,
            "target_identity": self.target_identity,
            "frame_id": self.frame_id,
            "surface_id": self.surface_id,
            "conflict": bool(self.conflict),
        }


def observer_evidence_semantics(
    source: EvidenceSource | str,
    coverage: EvidenceCoverage | str,
    *,
    freshness: EvidenceFreshness | str = EvidenceFreshness.FRESH,
    target_identity: str = "",
    surface_id: str = "",
    frame_id: str = "",
) -> dict:
    """Describe an observer's scope without inventing claim-specific facts."""

    canonical_source = _enum_value(source, EvidenceSource, EvidenceSource.OTHER)
    canonical_coverage = _enum_value(
        coverage, EvidenceCoverage, EvidenceCoverage.UNKNOWN,
    )
    canonical_freshness = _enum_value(
        freshness, EvidenceFreshness, EvidenceFreshness.UNKNOWN,
    )
    return {
        "schema_version": 1,
        "source": canonical_source.value,
        "coverage": canonical_coverage.value,
        "freshness": canonical_freshness.value,
        "target_identity": _safe_identifier(target_identity, 300),
        "surface_id": _safe_identifier(surface_id),
        "frame_id": _safe_identifier(frame_id),
        "missing_claim_status": ClaimStatus.UNKNOWN.value,
        "negative_claim_authority": "explicit_absent_verified_only",
    }


def arbitrate_claim_evidence(
    evidence: Iterable[ClaimEvidence],
    *,
    target_identity: str,
) -> ClaimEvidence:
    """Resolve fresh evidence for one target without a fixed source hierarchy."""

    target = _safe_identifier(target_identity, 300)
    items = [item for item in evidence if isinstance(item, ClaimEvidence)]
    claim = next((item.claim for item in items if item.claim), "claim")
    applicable = [
        item for item in items
        if target
        and item.freshness is EvidenceFreshness.FRESH
        and item.target_identity == target
    ]
    present = [item for item in applicable if item.status is ClaimStatus.PRESENT]
    absent = [
        item for item in applicable
        if item.status is ClaimStatus.ABSENT_VERIFIED
    ]
    if present and absent:
        strongest = max(
            [*present, *absent],
            key=lambda item: (item.observed_at, item.confidence),
        )
        return replace(
            strongest,
            status=ClaimStatus.CONFLICTING,
            value=None,
            absence_proven=False,
            conflict=True,
        )
    resolved = present or absent
    if resolved:
        return max(
            resolved,
            key=lambda item: (item.observed_at, item.confidence),
        )
    return ClaimEvidence(
        claim=claim,
        status=ClaimStatus.UNKNOWN,
        source=EvidenceSource.OTHER,
        coverage=EvidenceCoverage.UNKNOWN,
        freshness=EvidenceFreshness.FRESH,
        target_identity=target,
    )


__all__ = [
    "ClaimEvidence",
    "ClaimStatus",
    "EvidenceCoverage",
    "EvidenceFreshness",
    "EvidenceSource",
    "arbitrate_claim_evidence",
    "observer_evidence_semantics",
]
