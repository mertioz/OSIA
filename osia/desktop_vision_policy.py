"""Adaptive observation policy; visual actuation remains an execution layer."""

from __future__ import annotations

from dataclasses import dataclass

from osia.visual_observation import CaptureKind, ObservationReason


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


@dataclass(frozen=True)
class ObservationDecision:
    eligible: bool
    capture_kind: CaptureKind | None = None
    reason: ObservationReason | None = None
    reason_code: str = ""
    maximum_passes: int = 0


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
    elif evidence.custom_rendered:
        reason = ObservationReason.CUSTOM_RENDERED
        reason_code = "custom_rendered_surface"
    elif evidence.accessible_node_count <= 1:
        reason = ObservationReason.INCOMPLETE_ACCESSIBILITY
        reason_code = "accessibility_tree_incomplete"
    elif evidence.small_target:
        reason = ObservationReason.SMALL_TARGET
        reason_code = "small_or_dense_target"
    elif evidence.visual_postcondition and not evidence.structured_postcondition_complete:
        reason = ObservationReason.VERIFICATION
        reason_code = "visual_postcondition_adds_confidence"

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
    "DesktopEvidence",
    "ObservationDecision",
    "decide_visual_observation",
    "global_capture_fallback",
]
