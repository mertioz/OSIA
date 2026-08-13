"""Adaptive observation policy; visual actuation remains an execution layer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from osia.visual_observation import CaptureKind, ObservationReason


class AccessibilityQuality(str, Enum):
    """Target-relative semantic quality for one exact desktop window."""

    NO_TREE = "no_tree"
    WINDOW_ONLY = "window_only"
    PARTIAL = "partial"
    TARGET_MISSING = "target_missing"
    DELAYED = "delayed"
    INACCESSIBLE_SUBTREE = "inaccessible_subtree"
    CUSTOM_RENDERED = "custom_rendered"
    RICH_TARGET_RESOLVED = "rich_target_resolved"


@dataclass(frozen=True)
class DesktopEvidence:
    candidate_count: int = 0
    actionable_candidate_count: int = 0
    accessible_node_count: int = 0
    custom_rendered: bool = False
    popup_suspected: bool = False
    small_target: bool = False
    state_conflict: bool = False
    ambiguous_view: bool = False
    visual_postcondition: bool = False
    structured_postcondition_complete: bool = False
    local_region_available: bool = True
    scene_changed: bool = False
    target_requested: bool = False
    actionable_pattern_count: int = 0
    focus_observable: bool = False
    tree_observed: bool = True
    scene_window_exact: bool = True
    accessibility_quality: AccessibilityQuality | None = None


@dataclass(frozen=True)
class ObservationDecision:
    eligible: bool
    capture_kind: CaptureKind | None = None
    reason: ObservationReason | None = None
    reason_code: str = ""
    maximum_passes: int = 0


def classify_accessibility_quality(
    evidence: DesktopEvidence,
) -> AccessibilityQuality:
    """Classify accessibility relative to the requested semantic target.

    A populated tree is intentionally insufficient when none of its nodes
    resolves the requested target.  Conversely, one enabled target with an
    actionable pattern protects the rich-UIA fast path.
    """

    if evidence.accessibility_quality is not None:
        return evidence.accessibility_quality
    if not evidence.scene_window_exact:
        return AccessibilityQuality.INACCESSIBLE_SUBTREE
    if not evidence.tree_observed:
        return AccessibilityQuality.NO_TREE
    if evidence.accessible_node_count <= 0:
        return AccessibilityQuality.WINDOW_ONLY
    if evidence.custom_rendered:
        return AccessibilityQuality.CUSTOM_RENDERED
    if (
        evidence.scene_changed
        and evidence.target_requested
        and evidence.candidate_count > 0
    ):
        return AccessibilityQuality.DELAYED
    if evidence.target_requested and evidence.candidate_count == 0:
        if evidence.actionable_pattern_count <= 0:
            return AccessibilityQuality.CUSTOM_RENDERED
        return AccessibilityQuality.TARGET_MISSING
    if evidence.candidate_count > 0 and evidence.actionable_candidate_count <= 0:
        if evidence.actionable_pattern_count <= 0:
            return AccessibilityQuality.CUSTOM_RENDERED
        return AccessibilityQuality.INACCESSIBLE_SUBTREE
    if evidence.candidate_count == 1 and evidence.actionable_candidate_count == 1:
        return AccessibilityQuality.RICH_TARGET_RESOLVED
    return AccessibilityQuality.PARTIAL


def decide_visual_observation(
    evidence: DesktopEvidence,
    *,
    model_supports_vision: bool,
    budget_available: bool = True,
    existing_current_observation: bool = False,
    for_actuation: bool = False,
) -> ObservationDecision:
    """Choose the smallest useful observation without selecting an action path."""

    if not model_supports_vision:
        return ObservationDecision(False, reason_code="effective_model_non_vision")
    if not budget_available:
        return ObservationDecision(False, reason_code="observation_budget_exhausted")
    if existing_current_observation and not evidence.scene_changed:
        return ObservationDecision(False, reason_code="current_observation_reused")

    quality = classify_accessibility_quality(evidence)
    reason = None
    reason_code = "structured_evidence_sufficient"
    if evidence.state_conflict:
        reason = ObservationReason.STATE_CONFLICT
        reason_code = "structured_visual_state_conflict"
    elif evidence.popup_suspected:
        reason = ObservationReason.POPUP_OR_OVERLAY
        reason_code = "popup_or_overlay_suspected"
    elif evidence.candidate_count > 1 or evidence.ambiguous_view:
        reason = ObservationReason.AMBIGUITY
        reason_code = "multiple_semantic_candidates"
    elif quality is AccessibilityQuality.CUSTOM_RENDERED:
        reason = ObservationReason.CUSTOM_RENDERED
        reason_code = "custom_rendered_surface"
    elif evidence.small_target:
        reason = ObservationReason.SMALL_TARGET
        reason_code = "small_or_dense_target"
    elif evidence.visual_postcondition and not evidence.structured_postcondition_complete:
        reason = ObservationReason.VERIFICATION
        reason_code = "visual_postcondition_adds_confidence"
    elif quality in {
        AccessibilityQuality.NO_TREE,
        AccessibilityQuality.WINDOW_ONLY,
        AccessibilityQuality.PARTIAL,
        AccessibilityQuality.TARGET_MISSING,
        AccessibilityQuality.DELAYED,
        AccessibilityQuality.INACCESSIBLE_SUBTREE,
    }:
        reason = ObservationReason.INCOMPLETE_ACCESSIBILITY
        reason_code = quality.value

    if reason is None:
        return ObservationDecision(False, reason_code=reason_code)

    capture_kind = (
        CaptureKind.REGION
        if evidence.local_region_available
        and reason in {
            ObservationReason.AMBIGUITY,
            ObservationReason.SMALL_TARGET,
            ObservationReason.VERIFICATION,
        }
        else CaptureKind.WINDOW
    )
    maximum_passes = 1
    if for_actuation and evidence.small_target:
        capture_kind = CaptureKind.ZOOM_REFINE
        maximum_passes = 2
    return ObservationDecision(
        True,
        capture_kind=capture_kind,
        reason=reason,
        reason_code=reason_code,
        maximum_passes=maximum_passes,
    )


def global_capture_fallback(
    *,
    model_supports_vision: bool,
    local_capture_failed: bool,
    global_budget_available: bool,
) -> ObservationDecision:
    if not model_supports_vision:
        return ObservationDecision(False, reason_code="effective_model_non_vision")
    if not local_capture_failed:
        return ObservationDecision(False, reason_code="local_evidence_available")
    if not global_budget_available:
        return ObservationDecision(False, reason_code="global_capture_budget_exhausted")
    return ObservationDecision(
        True,
        capture_kind=CaptureKind.GLOBAL,
        reason=ObservationReason.INCOMPLETE_ACCESSIBILITY,
        reason_code="local_surface_capture_unavailable",
        maximum_passes=1,
    )


__all__ = [
    "AccessibilityQuality",
    "DesktopEvidence",
    "ObservationDecision",
    "classify_accessibility_quality",
    "decide_visual_observation",
    "global_capture_fallback",
]
