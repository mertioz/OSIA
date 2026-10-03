from __future__ import annotations

from io import BytesIO
from pathlib import Path
import hashlib
import time
import types

from PIL import Image

import osia.agent as agent_module
from osia import desktop_scene, model_capabilities
from osia.agent import OSIAAgent
from osia.desktop_scene import DesktopScene
from osia.desktop_surface import (
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
)
from osia.evidence_arbitration import (
    ClaimEvidence,
    ClaimStatus,
    EvidenceCoverage,
    EvidenceFreshness,
    EvidenceSource,
    arbitrate_claim_evidence,
)
from osia.turn_context import (
    fixed_turn_context,
    resolve_turn_context,
    response_language_drift,
)
from osia.vision_runtime import (
    CoordinateTransform,
    NormalizedBBox,
    VisionDomain,
    VisionStage,
    VisualTarget,
    crop_capture,
    prepare_capture,
)
from osia.visual_observation import (
    CaptureKind,
    ObservationReason,
    VisualObservationStore,
)


def _evidence(
    status: ClaimStatus,
    source: EvidenceSource,
    *,
    coverage: EvidenceCoverage,
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH,
    target: str = "application:generation",
    absence_proven: bool = False,
    confidence: float = 0.9,
    observed_at: float = 1.0,
) -> ClaimEvidence:
    return ClaimEvidence(
        claim="current visible item",
        status=status,
        source=source,
        coverage=coverage,
        freshness=freshness,
        target_identity=target,
        absence_proven=absence_proven,
        confidence=confidence,
        observed_at=observed_at,
    )


def _png(width: int = 1600, height: int = 900, color=(24, 48, 72)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _frame():
    return prepare_capture(
        _png(),
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        application_id="custom-rendered-app",
        surface_ref="surface-current",
        window_generation="generation-current",
        structural_epoch=7,
        transform=CoordinateTransform(
            source_width=1600,
            source_height=900,
            source_box=(0.0, 0.0, 1600.0, 900.0),
            screen_bounds=(100, 80, 1700, 980),
            client_origin_screen=(100, 80),
            dpi_scale=1.5,
        ),
        native_window_token=hashlib.sha256(b"41:101").hexdigest()[:24],
        capture_method="windows_graphics_capture",
        exact_surface_pixels=True,
    )


def _history(language: str, text: str) -> list[dict]:
    return [
        {"role": "user", "content": text, "_language": language},
        {"role": "assistant", "content": "prior answer", "_language": language},
    ]


def _run_language_rounds(rounds, *, context, user_text="Explique la situation."):
    agent = OSIAAgent(lang=context.language)
    iterator = iter(rounds)
    calls = []

    def stream(self, _callback, *, on_first_token=None, guard=None):
        calls.append((self.provider, self.model, self._wire_messages()))
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    agent._stream_with_sentences = types.MethodType(stream, agent)
    final = agent.run(
        user_text,
        on_sentence=lambda _text: None,
        turn_context=context,
    )
    return agent, final, calls


# Evidence cases 1-11.


def test_01_partial_uia_omission_is_unknown_not_absent():
    item = _evidence(
        ClaimStatus.ABSENT_VERIFIED,
        EvidenceSource.UIA,
        coverage=EvidenceCoverage.PARTIAL,
        absence_proven=True,
    )
    assert item.status is ClaimStatus.UNKNOWN


def test_02_complete_explicit_search_can_verify_absence():
    item = _evidence(
        ClaimStatus.ABSENT_VERIFIED,
        EvidenceSource.APPLICATION_STATE,
        coverage=EvidenceCoverage.COMPLETE,
        absence_proven=True,
    )
    assert item.status is ClaimStatus.ABSENT_VERIFIED


def test_03_partial_dom_omission_is_unknown():
    item = _evidence(
        ClaimStatus.ABSENT_VERIFIED,
        EvidenceSource.DOM,
        coverage=EvidenceCoverage.PARTIAL,
        absence_proven=True,
    )
    assert item.status is ClaimStatus.UNKNOWN


def test_04_positive_visual_evidence_beats_structured_unknown():
    result = arbitrate_claim_evidence(
        [
            _evidence(
                ClaimStatus.UNKNOWN,
                EvidenceSource.UIA,
                coverage=EvidenceCoverage.PARTIAL,
            ),
            _evidence(
                ClaimStatus.PRESENT,
                EvidenceSource.VISION,
                coverage=EvidenceCoverage.PARTIAL,
                confidence=0.98,
            ),
        ],
        target_identity="application:generation",
    )
    assert result.status is ClaimStatus.PRESENT
    assert result.source is EvidenceSource.VISION


def test_05_stale_visual_evidence_cannot_override_fresh_state():
    result = arbitrate_claim_evidence(
        [
            _evidence(
                ClaimStatus.PRESENT,
                EvidenceSource.VISION,
                coverage=EvidenceCoverage.PARTIAL,
                freshness=EvidenceFreshness.STALE,
                confidence=1.0,
            ),
            _evidence(
                ClaimStatus.ABSENT_VERIFIED,
                EvidenceSource.APPLICATION_STATE,
                coverage=EvidenceCoverage.COMPLETE,
                absence_proven=True,
            ),
        ],
        target_identity="application:generation",
    )
    assert result.status is ClaimStatus.ABSENT_VERIFIED


def test_06_other_target_evidence_is_rejected():
    result = arbitrate_claim_evidence(
        [
            _evidence(
                ClaimStatus.PRESENT,
                EvidenceSource.VISION,
                coverage=EvidenceCoverage.PARTIAL,
                target="other-application:generation",
            )
        ],
        target_identity="application:generation",
    )
    assert result.status is ClaimStatus.UNKNOWN


def test_07_exact_conflict_stays_conflicting_for_refinement():
    result = arbitrate_claim_evidence(
        [
            _evidence(
                ClaimStatus.PRESENT,
                EvidenceSource.VISION,
                coverage=EvidenceCoverage.PARTIAL,
            ),
            _evidence(
                ClaimStatus.ABSENT_VERIFIED,
                EvidenceSource.APPLICATION_STATE,
                coverage=EvidenceCoverage.COMPLETE,
                absence_proven=True,
            ),
        ],
        target_identity="application:generation",
    )
    assert result.status is ClaimStatus.CONFLICTING
    assert result.conflict is True


def test_08_full_frame_can_be_refined_to_higher_detail_png():
    full = _frame()
    refined = crop_capture(
        full,
        NormalizedBBox(left=0.42, top=0.38, right=0.58, bottom=0.62),
    )
    assert refined is not full
    assert refined.mime == "image/png"
    assert refined.capture_method == "adaptive_crop"
    assert min(refined.image_width, refined.image_height) >= 768
    assert refined.transform.source_box != full.transform.source_box
    assert refined.native_window_token == full.native_window_token


def test_09_clear_full_frame_does_not_trigger_an_automatic_refine():
    full = _frame()
    unchanged = crop_capture(
        full,
        NormalizedBBox(left=0.0, top=0.0, right=1.0, bottom=1.0),
    )
    store = VisualObservationStore()
    assert unchanged is full
    assert store.refinement_count == 0


def test_10_two_refinement_passes_are_the_hard_bound():
    store = VisualObservationStore(maximum_captures=10, maximum_refinements=2)
    for index, payload in enumerate((b"refine-one", b"refine-two"), start=1):
        assert store.budget_allows(CaptureKind.ZOOM_REFINE)
        store.add(
            surface_ref="surface-current",
            capture_kind=CaptureKind.ZOOM_REFINE,
            reason=ObservationReason.SMALL_TARGET,
            width=800,
            height=768,
            capture_epoch=index,
            structural_epoch=7,
            surface_fingerprint="same-target",
            png_bytes=payload,
        )
    assert store.refinement_count == 2
    assert store.budget_allows(CaptureKind.ZOOM_REFINE) is False


def test_10b_visual_tool_executes_two_target_local_refinements(monkeypatch):
    monkeypatch.setattr(
        model_capabilities,
        "_exact_account_metadata",
        lambda _model: {
            "id": "gpt-5.6-terra",
            "provider": "chatgpt_account",
            "inputModalities": ["text", "image"],
        },
    )
    bounds = SurfaceBounds(100, 80, 1700, 980)
    window = DesktopSurfaceRef(
        ref_id="surface-current",
        application_id="custom-rendered-app",
        process_ref="process-current",
        window_generation="generation-current",
        surface_kind=SurfaceKind.WINDOW,
        role="WindowControl",
        accessible_label="Custom rendered window",
        bounds=bounds,
        structural_epoch=7,
    )
    scene = DesktopScene(
        application_id="custom-rendered-app",
        window_ref=window,
        surfaces=(window,),
        structural_epoch=7,
        scene_signature="scene-current",
        accessibility_complete=False,
        observed_at=time.time(),
        foreground=True,
    )
    native = NativeSurfaceIdentity(41, 1000.0, 101, class_name="FixtureClass")
    monkeypatch.setattr(
        desktop_scene,
        "_observe_exact_bound_visual_scene",
        lambda *_args, **_kwargs: (scene, window, ""),
    )
    monkeypatch.setattr(
        desktop_scene.DESKTOP_SURFACE_REGISTRY,
        "native_identity",
        lambda _ref: native,
    )
    monkeypatch.setattr(
        desktop_scene,
        "_visual_target_for_scene",
        lambda *_args: VisualTarget(
            application_id="custom-rendered-app",
            domain=VisionDomain.DESKTOP,
            surface_id="surface-current",
            surface_generation="generation-current",
            bounds=(100, 80, 1700, 980),
            native_window_id="101",
        ),
    )

    class Runtime:
        def __init__(self, provider, model):
            assert (provider, model) == ("chatgpt_account", "gpt-5.6-terra")

        def resolve_target(self, _semantic, resolver, *, trace):
            trace.record(VisionStage.TARGET_RESOLVED, "fixture_target")
            return resolver(""), trace

        def capture(self, _target, _driver, *, trace):
            trace.record(VisionStage.CAPTURE_REQUESTED, "fixture_capture")
            trace.record(VisionStage.CAPTURE_COMPLETED, "fixture_captured")
            return _frame(), trace

    monkeypatch.setattr(desktop_scene, "VisionRuntime", Runtime)
    route = desktop_scene.bind_effective_model(
        "chatgpt_account", "gpt-5.6-terra",
    )
    turn = desktop_scene.begin_desktop_turn()
    try:
        initial = desktop_scene.request_visual_observation(
            window.ref_id, "custom_rendered", "central detail",
        )
        first_refine = desktop_scene.request_visual_observation(
            window.ref_id,
            "small_target",
            "central detail",
            visual_frame_id=initial["receipt"]["frame_id"],
            visual_bbox={"left": .3, "top": .3, "right": .7, "bottom": .7},
        )
        second_refine = desktop_scene.request_visual_observation(
            window.ref_id,
            "small_target",
            "central detail",
            visual_frame_id=first_refine["receipt"]["frame_id"],
            visual_bbox={"left": .4, "top": .4, "right": .6, "bottom": .6},
        )
        blocked = desktop_scene.request_visual_observation(
            window.ref_id,
            "small_target",
            "central detail",
            visual_frame_id=second_refine["receipt"]["frame_id"],
            visual_bbox={"left": .4, "top": .4, "right": .6, "bottom": .6},
        )
    finally:
        desktop_scene.end_desktop_turn(turn)
        desktop_scene.reset_effective_model(route)

    assert initial["receipt"]["source_frame_id"] == ""
    assert first_refine["receipt"]["source_frame_id"] == initial["receipt"]["frame_id"]
    assert first_refine["receipt"]["refine_count"] == 1
    assert second_refine["receipt"]["source_frame_id"] == first_refine["receipt"]["frame_id"]
    assert second_refine["receipt"]["refine_count"] == 2
    assert first_refine["receipt"]["crop_rectangle"]
    assert second_refine["receipt"]["capture_method"] == "adaptive_crop"
    assert blocked["receipt"]["reason_code"] == "CAPTURE_BUDGET_EXHAUSTED"


def test_11_production_evidence_logic_contains_no_reproducer_specific_branch():
    root = Path(__file__).resolve().parents[1]
    production = "\n".join(
        (root / relative).read_text(encoding="utf-8").casefold()
        for relative in (
            "osia/evidence_arbitration.py",
            "osia/desktop_scene.py",
            "osia/visual_observation.py",
        )
    )
    assert "brawlhalla" not in production
    assert "steam" not in production


# Language cases 12-26.


def test_12_french_current_message_selects_french_preference():
    context = resolve_turn_context("Peux-tu détailler clairement ce résultat ?")
    assert (context.language, context.source) == ("fr", "current_request")


def test_13_english_current_message_switches_immediately_after_french_history():
    context = resolve_turn_context(
        "Now explain the second point in a short answer.",
        history=_history("fr", "Explique-moi le résultat."),
    )
    assert context.language == "en"
    assert context.source == "current_request"


def test_14_french_current_message_switches_immediately_after_english_history():
    context = resolve_turn_context(
        "Et maintenant détaille clairement le deuxième point.",
        history=_history("en", "Explain the result in English."),
    )
    assert context.language == "fr"
    assert context.source == "current_request"


def test_15_explicit_language_request_overrides_message_detection():
    context = resolve_turn_context("Maintenant, answer in English please.")
    assert context.language == "en"
    assert context.source == "explicit_request"


def test_16_neutral_followup_softly_uses_recent_user_language():
    context = resolve_turn_context(
        "continue",
        history=_history("es", "Explica los detalles importantes, por favor."),
    )
    assert context.language == "es"
    assert context.source == "recent_user_context"


def test_17_foreign_ui_labels_do_not_select_the_turn_language():
    context = resolve_turn_context(
        "Explique-moi en détail la page avec Library Filters et Play State."
    )
    assert context.language == "fr"


def test_18_markdown_menu_names_do_not_invalidate_french_prose():
    context = fixed_turn_context("fr", source="current_request")
    answer = (
        "La fenêtre montre les éléments demandés.\n\n"
        "- **Library Filters** : le filtre visible est actif.\n"
        "- **Hardware Monitoring** : les mesures sont affichées."
    )
    assert response_language_drift(answer, context) is None


def test_19_tool_receipts_do_not_select_response_language():
    history = [
        {"role": "user", "content": "Explique-moi le résultat.", "_language": "fr"},
        {"role": "tool", "content": "outcome=observed status=ready"},
        {"role": "assistant", "content": "Library Filters Play State", "_language": "en"},
    ]
    context = resolve_turn_context("continue", history=history)
    assert (context.language, context.source) == ("fr", "recent_user_context")


def test_20_activity_or_reasoning_text_does_not_select_response_language():
    history = [
        {"role": "user", "content": "Peux-tu continuer en détail ?", "_language": "fr"},
        {"role": "assistant", "content": "Thinking... Inspecting UI...", "_language": "en"},
    ]
    context = resolve_turn_context("continue", history=history)
    assert context.language == "fr"


def test_21_no_conversation_wide_language_lock():
    history = _history("fr", "Explique-moi cela en français.")
    english = resolve_turn_context(
        "Now give me the short version in English.", history=history,
    )
    history += _history("en", "Now give me the short version in English.")
    french = resolve_turn_context(
        "Et maintenant détaille le deuxième point.", history=history,
    )
    assert english.language == "en"
    assert french.language == "fr"


def test_22_unresolved_language_is_natural_and_never_hard_validated():
    context = resolve_turn_context("👍", history=[])
    assert context.source == "natural_model"
    assert response_language_drift("Una respuesta natural y útil.", context) is None


def test_23_wrong_language_candidate_gets_one_same_model_repair():
    context = fixed_turn_context("fr", source="current_request")
    agent, final, calls = _run_language_rounds(
        (
            "The visible application is ready and the current state is stable.",
            "L’application visible est prête et son état actuel est stable.",
        ),
        context=context,
    )
    assert final.startswith("L’application visible")
    assert len(calls) == 2
    assert calls[0][:2] == calls[1][:2]
    assert agent.last_run_diagnostic is None


def test_24_imperfect_or_failed_repair_still_returns_useful_answer():
    context = fixed_turn_context("fr", source="current_request")
    original = "The visible application is ready and the current state is stable."
    for repair in (
        "The second rendering is still in the wrong response language.",
        RuntimeError("provider presentation repair failed"),
    ):
        agent, final, calls = _run_language_rounds(
            (original, repair), context=context,
        )
        assert final == original
        assert len(calls) == 2
        assert agent.last_run_diagnostic["reason_code"] == (
            "language_presentation_repair_incomplete"
        )
        assert agent.last_run_diagnostic["delivered_useful_candidate"] is True


def test_25_language_repair_cannot_execute_a_tool(monkeypatch):
    executions = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: executions.append("unexpected") or "unexpected",
    )
    context = fixed_turn_context("fr", source="current_request")
    original = "The visible application is ready and the current state is stable."
    _agent, final, calls = _run_language_rounds(
        (original, '<tool_call>{"tool":"get_system_info"}</tool_call>'),
        context=context,
    )
    assert final == original
    assert len(calls) == 2
    assert executions == []


def test_26_language_repair_cannot_replay_a_mutation(monkeypatch):
    mutations = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda **kwargs: mutations.append(kwargs) or "unexpected",
    )
    context = fixed_turn_context("fr", source="current_request")
    original = "The requested file was written and its result was verified."
    _agent, final, calls = _run_language_rounds(
        (
            original,
            '<tool_call>{"tool":"write_file","path":"fixture.txt",'
            '"content":"do not replay"}</tool_call>',
        ),
        context=context,
    )
    assert final == original
    assert len(calls) == 2
    assert mutations == []
