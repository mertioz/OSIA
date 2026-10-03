"""WOR-266 visual-modality and generic multi-HWND surface regressions."""

from __future__ import annotations

import inspect
import io
import time
from dataclasses import replace
from types import SimpleNamespace

from PIL import Image, ImageDraw

from osia.agent import OSIAAgent
from osia.desktop_scene import (
    NativeWindowSurfaceCandidate,
    SurfaceCoverage,
    _native_window_surface_candidates,
    select_application_visual_surface,
)
from osia.desktop_surface import SurfaceBounds
from osia.execution_strategy import (
    SemanticActionNode,
    build_execution_strategy,
    is_desktop_visual_read_mission,
    record_tool_observation,
)
from osia.goal_contract import (
    EvidenceRequirement,
    evidence_requirement_for_node,
    goal_contract_from_semantic_mission,
)
from osia.vision_runtime import (
    CoordinateTransform,
    NormalizedBBox,
    VisionDomain,
    crop_capture,
    prepare_capture,
)
from osia.visual_observation import (
    CaptureKind,
    ObservationReason,
    VisualObservationStore,
)


VISUAL_TOOLS = (
    "inspect_desktop_surfaces",
    "request_visual_observation",
    "desktop_click",
)


def _contract(request: str):
    strategy = build_execution_strategy(request, VISUAL_TOOLS)
    return strategy, goal_contract_from_semantic_mission(strategy.mission)


def _record_image(
    strategy,
    *,
    sent: bool = True,
    consumed: bool = True,
    coverage: str = "full_window",
):
    requested_application = str(strategy.goal.required_application or "")
    observed_application = (
        "dcv2" if requested_application == "msi center" else
        requested_application or "fixture"
    )
    return record_tool_observation(
        "request_visual_observation",
        {
            "text": "exact image prepared",
            "receipt": {
                "outcome": "observed",
                "reason_code": (
                    "IMAGE_INPUT_SENT" if sent else
                    "ACTIVE_MODEL_IMAGE_PART_BUILT"
                ),
                "surface_ref": "surface-main",
                "window_surface_ref": "surface-main",
                "application": observed_application,
                "requested_application": requested_application,
                "resolved_target_candidates": [
                    {"value": observed_application},
                    {"value": requested_application},
                ],
                "target_conflict": False,
                "window_generation": "generation-main",
                "frame_id": "frame-main",
                "exact_surface_pixels": True,
                "image_input_sent": sent,
                "image_input_consumed": sent and consumed,
                "content_coverage": coverage,
                "dimensions": {"width": 1200, "height": 800},
                "refine_count": 0,
                "provider_id": "chatgpt_account",
                "model_id": "gpt-5.6-terra",
            },
        },
        is_error=False,
        strategy=strategy,
    )


def _candidate(
    hwnd: int,
    *,
    root: int = 100,
    parent: int = 100,
    owner: int = 0,
    bounds: SurfaceBounds | None = None,
    client: SurfaceBounds | None = None,
    content: bool = False,
    popup: bool = False,
    tool: bool = False,
    foreground: bool = False,
    stale: bool = False,
    pid: int = 41,
):
    bounds = bounds or SurfaceBounds(0, 0, 1200, 800)
    return NativeWindowSurfaceCandidate(
        hwnd=hwnd,
        pid=pid,
        process_started_at=1000.0,
        class_name=f"Class{hwnd}",
        title=f"Surface {hwnd}",
        parent_hwnd=parent,
        owner_hwnd=owner,
        root_hwnd=root,
        is_window=True,
        visible=True,
        cloaked=False,
        bounds=bounds,
        client_bounds=client or bounds,
        monitor="monitor-1",
        dpi=120,
        surface_id=f"surface-{hwnd}",
        generation=f"generation-{hwnd}",
        uia_control_type=("DocumentControl" if content else "PaneControl"),
        content_host_signal=content,
        tool_window=tool,
        popup_window=popup,
        foreground=foreground,
        stale=stale,
    )


def _multi_hwnd_fixture(*, foreground: bool = True):
    root = _candidate(
        100,
        root=100,
        parent=0,
        bounds=SurfaceBounds(0, 0, 1200, 800),
        client=SurfaceBounds(0, 40, 1200, 800),
        foreground=foreground,
    )
    toolbar = _candidate(
        101,
        bounds=SurfaceBounds(0, 0, 1200, 70),
        client=SurfaceBounds(0, 0, 1200, 70),
    )
    content = _candidate(
        102,
        bounds=SurfaceBounds(0, 80, 1200, 800),
        client=SurfaceBounds(0, 80, 1200, 800),
        content=True,
    )
    popup = _candidate(
        103,
        owner=100,
        bounds=SurfaceBounds(400, 200, 800, 500),
        popup=True,
    )
    tool = _candidate(
        104,
        bounds=SurfaceBounds(20, 20, 450, 260),
        tool=True,
    )
    unrelated = _candidate(
        200,
        root=200,
        parent=0,
        bounds=SurfaceBounds(-50, -50, 2000, 1400),
        client=SurfaceBounds(-50, -50, 2000, 1400),
        content=True,
    )
    return root, toolbar, content, popup, tool, unrelated


def test_cross_process_content_host_is_kept_when_exact_root_identity_matches():
    root, toolbar, content, popup, tool, unrelated = _multi_hwnd_fixture()
    renderer = replace(content, pid=9090, process_started_at=2000.0)
    selection = select_application_visual_surface(
        (root, toolbar, renderer, popup, tool, unrelated),
        root.hwnd,
    )
    assert selection.capture is root
    assert selection.content is renderer
    assert selection.coverage is SurfaceCoverage.FULL_WINDOW


def _central_detail_frame():
    image = Image.new("RGB", (1200, 800), (12, 24, 40))
    draw = ImageDraw.Draw(image)
    draw.rectangle((520, 350, 680, 450), fill=(240, 30, 180))
    draw.text((545, 390), "BRAWLHALLA", fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    transform = CoordinateTransform(
        source_width=1200,
        source_height=800,
        source_box=(0.0, 0.0, 1200.0, 800.0),
        screen_bounds=(100, 80, 1300, 880),
        client_origin_screen=(100, 80),
        window_bounds=(100, 80, 1300, 880),
        client_bounds=(0, 0, 1200, 800),
    )
    return prepare_capture(
        output.getvalue(),
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        application_id="fixture",
        surface_ref="surface-main",
        window_generation="generation-main",
        structural_epoch=7,
        transform=transform,
        native_window_token="native-main",
        capture_method="fixture_exact_window",
        exact_surface_pixels=True,
        domain=VisionDomain.DESKTOP,
        window_bounds=(100, 80, 1300, 880),
        client_bounds=(0, 0, 1200, 800),
        screen_origin=(100, 80),
    )


def test_01_what_do_you_see_requires_visual_evidence():
    strategy, contract = _contract("What do you see on the Steam window?")
    assert contract.nodes[0].evidence_requirement == "visual"
    assert is_desktop_visual_read_mission(strategy)


def test_02_background_question_requires_visual_evidence():
    _strategy, contract = _contract(
        "What is the background theme on the MSI Center window?"
    )
    assert contract.nodes[0].evidence_requirement == "visual"


def test_03_color_and_visible_target_require_visual_or_mixed_evidence():
    _strategy, contract = _contract(
        "What color is the visible button in the app window?"
    )
    assert contract.nodes[0].evidence_requirement == "visual"
    node = SemanticActionNode(
        target_object="visible blue button",
        required_application="fixture",
        required_surface="",
        required_account="",
        requested_action="click",
        observable_success_postcondition="button activated",
        fingerprint="mixed-fixture",
        requires_execution=True,
        source_clause="Click the visible blue button",
    )
    assert evidence_requirement_for_node(node) is EvidenceRequirement.MIXED


def test_04_exact_cpu_question_is_structured_not_visual():
    strategy, contract = _contract("What is the exact CPU usage?")
    assert contract.nodes[0].evidence_requirement == "structured"
    assert not is_desktop_visual_read_mission(strategy)


def test_05_uia_only_cannot_terminally_satisfy_visual_goal():
    strategy, _contract_value = _contract(
        "What do you see on the Steam window?"
    )
    postcondition = strategy.required_postconditions[0]
    scene = {
        "application": "steam",
        "requested_application": "steam",
        "window_surface_ref": "surface-main",
        "window_generation": "generation-main",
        "target_resolution": "exact_target",
        "observed_at": time.time(),
        "surfaces": [{"label": "STORE"}, {"label": "LIBRARY"}],
    }
    record_tool_observation(
        "inspect_desktop_surfaces",
        {
            "scene": scene,
            "receipt": {
                "outcome": "confirmed_success",
                "reason_code": "exact_final_state_observation",
                "postcondition": postcondition,
                "observed_at": time.time(),
            },
        },
        is_error=False,
        strategy=strategy,
    )
    strategy.record_external_event(
        "inspect_desktop_surfaces",
        {"target": "steam"},
        outcome="success_verified",
        evidence="uia-only",
    )
    assert postcondition not in strategy.proven_postconditions


def test_06_consumed_exact_image_and_descriptive_answer_can_satisfy_visual_goal():
    strategy, _contract_value = _contract(
        "What do you see on the Steam window?"
    )
    assert _record_image(strategy) is False
    postcondition = strategy.required_postconditions[0]
    proven = strategy.prove_visual_answer(
        "The Steam window shows a dark library page with large game artwork.",
        evidence="provider-answer",
        provider="chatgpt_account",
        model="gpt-5.6-terra",
    )
    assert proven == (postcondition,)
    assert "visual_input:frame-main" in strategy.proven_postconditions[
        postcondition
    ]


def test_07_capture_protected_produces_honest_visual_unavailable_result():
    strategy, _contract_value = _contract(
        "What do you see on the Steam window?"
    )
    record_tool_observation(
        "request_visual_observation",
        {
            "text": "ERROR: protected",
            "receipt": {
                "outcome": "confirmed_failure_before_effect",
                "reason_code": "CAPTURE_PROTECTED",
            },
        },
        is_error=True,
        strategy=strategy,
    )
    assert not strategy.proven_postconditions
    agent = object.__new__(OSIAAgent)
    agent.lang = "en"
    final = agent._terminal_final_text(
        "blocked_verified",
        "",
        snapshot={
            "strategy_snapshot": strategy.snapshot(),
            "typed_evidence": [{"reason_code": "CAPTURE_PROTECTED"}],
        },
    )
    assert "visual observation was not reliably available" in final
    assert "not substitute accessibility" in final


def test_08_nonvision_route_cannot_fake_a_visual_claim():
    strategy, _contract_value = _contract(
        "What do you see on the Steam window?"
    )
    _record_image(strategy)
    assert strategy.prove_visual_answer(
        "I see a detailed game page in the window.",
        evidence="text-only-provider",
        provider="ollama",
        model="text-only-model",
    ) == ()
    assert not strategy.proven_postconditions


def test_09_accessibility_support_is_retained_without_impersonating_pixels():
    strategy, _contract_value = _contract(
        "What do you see on the Steam window?"
    )
    scene = {
        "application": "steam",
        "requested_application": "steam",
        "window_surface_ref": "surface-main",
        "window_generation": "generation-main",
        "target_resolution": "exact_target",
        "observed_at": time.time(),
        "surfaces": [{"label": "Steam"}, {"label": "LIBRARY"}],
    }
    record_tool_observation(
        "inspect_desktop_surfaces",
        {"scene": scene, "receipt": {"outcome": "observed"}},
        is_error=False,
        strategy=strategy,
    )
    assert strategy._latest_desktop_observation["uia_frontier_observed"] is True
    assert not strategy.proven_postconditions


def test_10_nonvisual_questions_do_not_enable_a_global_vision_first_rule():
    strategy, contract = _contract("Is the API connected?")
    assert contract.nodes[0].evidence_requirement == "structured"
    assert not is_desktop_visual_read_mission(strategy)
    assert "observation_requirement=visual" not in strategy.model_instruction()


def test_11_full_window_request_never_treats_narrow_toolbar_as_complete():
    candidates = _multi_hwnd_fixture()
    selection = select_application_visual_surface(candidates, 101)
    assert selection.capture.hwnd == 100
    assert selection.content.hwnd == 102
    assert selection.capture.hwnd != 101
    assert selection.coverage is SurfaceCoverage.FULL_WINDOW


def test_12_root_capture_and_large_content_host_are_selected_together():
    candidates = _multi_hwnd_fixture()
    selection = select_application_visual_surface(candidates, 102)
    assert (selection.capture.hwnd, selection.content.hwnd) == (100, 102)


def test_13_parent_owner_root_ancestry_is_preserved_in_diagnostics():
    candidates = _multi_hwnd_fixture()
    selection = select_application_visual_surface(candidates, 101)
    safe = selection.safe_dict(
        candidates,
        application_bounds=candidates[0].bounds,
    )
    toolbar = next(item for item in safe["candidates"] if item["hwnd"] == "101")
    popup = next(item for item in safe["candidates"] if item["hwnd"] == "103")
    assert toolbar["parent_hwnd"] == "100"
    assert toolbar["root_hwnd"] == "100"
    assert popup["owner_hwnd"] == "100"


def test_14_popup_and_tool_windows_are_excluded_from_primary_content():
    candidates = _multi_hwnd_fixture()
    selection = select_application_visual_surface(candidates, 101)
    assert selection.capture.hwnd not in {103, 104}
    assert selection.content.hwnd not in {103, 104}


def test_15_selected_frame_geometry_represents_meaningful_application_area():
    candidates = _multi_hwnd_fixture()
    selection = select_application_visual_surface(candidates, 101)
    capture_bounds = selection.capture.bounds
    content_bounds = selection.content.bounds
    assert (capture_bounds.width, capture_bounds.height) == (1200, 800)
    assert content_bounds.height == 720
    assert content_bounds.height > candidates[1].bounds.height * 8
    safe = selection.safe_dict(
        candidates,
        application_bounds=capture_bounds,
        captured_dimensions={"width": 1200, "height": 800},
    )
    assert safe["captured_frame_dimensions"] == {
        "width": 1200,
        "height": 800,
    }
    attempted = [
        item["hwnd"] for item in safe["candidates"]
        if item["capture_attempted"]
    ]
    assert attempted == ["100"]


def test_16_nonforeground_main_window_remains_read_only_capturable_target():
    candidates = _multi_hwnd_fixture(foreground=False)
    selection = select_application_visual_surface(candidates, 101)
    assert selection.capture.hwnd == 100
    assert selection.capture.foreground is False


def test_17_moved_resized_root_refreshes_without_changing_target_identity():
    candidates = _multi_hwnd_fixture()
    moved = tuple(
        replace(
            item,
            bounds=SurfaceBounds(200, 100, 1800, 1100),
            client_bounds=SurfaceBounds(200, 140, 1800, 1100),
        ) if item.hwnd == 100 else
        replace(
            item,
            bounds=SurfaceBounds(200, 180, 1800, 1100),
            client_bounds=SurfaceBounds(200, 180, 1800, 1100),
        ) if item.hwnd == 102 else item
        for item in candidates
    )
    selection = select_application_visual_surface(moved, 101)
    assert selection.capture.hwnd == 100
    assert selection.capture.bounds == SurfaceBounds(200, 100, 1800, 1100)


def test_18_stale_child_never_redirects_to_unrelated_same_process_sibling():
    root, toolbar, content, popup, tool, unrelated = _multi_hwnd_fixture()
    stale_toolbar = replace(toolbar, stale=True)
    selection = select_application_visual_surface(
        (root, stale_toolbar, content, popup, tool, unrelated),
        stale_toolbar.hwnd,
    )
    assert selection.capture.hwnd == 100
    assert selection.capture.hwnd != unrelated.hwnd


def test_19_largest_window_is_not_an_unsafe_authoritative_heuristic():
    candidates = _multi_hwnd_fixture()
    unrelated = candidates[-1]
    assert unrelated.pixel_area > candidates[0].pixel_area
    selection = select_application_visual_surface(candidates, 101)
    assert selection.capture.hwnd == 100
    assert selection.capture.root_hwnd == 100


def test_20_surface_selection_has_no_app_specific_identifiers():
    source = (
        inspect.getsource(select_application_visual_surface)
        + inspect.getsource(_native_window_surface_candidates)
    ).casefold()
    for identifier in ("steam", "electron", "chromium", "brawlhalla"):
        assert identifier not in source


def test_21_correct_full_frame_exposes_bounded_central_refinement():
    full = _central_detail_frame()
    refined = crop_capture(
        full,
        NormalizedBBox(left=0.43, top=0.40, right=0.57, bottom=0.60),
    )
    assert refined is not full
    assert refined.capture_method == "adaptive_crop"
    assert min(refined.image_width, refined.image_height) >= 768
    strategy, _contract_value = _contract(
        "What do you see on the Steam window?"
    )
    _record_image(strategy)
    record_tool_observation(
        "request_visual_observation",
        {
            "receipt": {
                "outcome": "observed",
                "reason_code": "IMAGE_INPUT_SENT",
                "surface_ref": "surface-main",
                "window_surface_ref": "surface-main",
                "application": "steam",
                "window_generation": "generation-main",
                "frame_id": "frame-refined",
                "source_frame_id": "frame-main",
                "exact_surface_pixels": True,
                "image_input_sent": True,
                "image_input_consumed": True,
                "content_coverage": "region",
                "dimensions": {"width": 800, "height": 768},
                "refine_count": 1,
                "provider_id": "chatgpt_account",
                "model_id": "gpt-5.6-terra",
            },
        },
        is_error=False,
        strategy=strategy,
    )
    postcondition = strategy.required_postconditions[0]
    assert strategy.prove_visual_answer(
        "The central Steam content shows a large game artwork and title.",
        evidence="provider-answer-refined",
        provider="chatgpt_account",
        model="gpt-5.6-terra",
    ) == (postcondition,)
    assert "visual_input:frame-refined" in strategy.proven_postconditions[
        postcondition
    ]


def test_22_roi_transform_remains_bound_to_original_physical_window():
    full = _central_detail_frame()
    bbox = NormalizedBBox(left=0.4, top=0.35, right=0.6, bottom=0.65)
    refined = crop_capture(full, bbox)
    assert refined.transform.source_box != full.transform.source_box
    assert refined.transform.window_bounds == full.transform.window_bounds
    mapped = full.transform.bbox_screen_pixels(bbox)
    assert mapped == (580, 360, 820, 600)


def test_23_central_game_detail_becomes_dominant_in_refined_fixture():
    full = _central_detail_frame()
    refined = crop_capture(
        full,
        NormalizedBBox(left=0.43, top=0.40, right=0.57, bottom=0.60),
    )

    def magenta_ratio(frame) -> float:
        with Image.open(io.BytesIO(frame.image_bytes)) as image:
            converted = image.convert("RGB")
            pixels = list(converted.get_flattened_data())
        magenta = sum(
            red > 180 and blue > 120 and green < 100
            for red, green, blue in pixels
        )
        return magenta / float(len(pixels))

    assert magenta_ratio(refined) > magenta_ratio(full) * 5


def test_24_initial_plus_two_refinements_is_the_hard_budget():
    store = VisualObservationStore(
        maximum_captures=3,
        maximum_refinements=2,
    )
    store.add(
        surface_ref="surface-main",
        capture_kind=CaptureKind.WINDOW,
        reason=ObservationReason.CUSTOM_RENDERED,
        width=1200,
        height=800,
        capture_epoch=0,
        structural_epoch=7,
        surface_fingerprint="main",
        png_bytes=b"initial",
    )
    for index in (1, 2):
        store.add(
            surface_ref="surface-main",
            capture_kind=CaptureKind.ZOOM_REFINE,
            reason=ObservationReason.SMALL_TARGET,
            width=800,
            height=768,
            capture_epoch=index,
            structural_epoch=7,
            surface_fingerprint="main",
            png_bytes=f"refine-{index}".encode(),
        )
    assert store.capture_count == 3
    assert store.refinement_count == 2
    assert not store.budget_allows(CaptureKind.ZOOM_REFINE)


def test_25_no_refinement_when_full_frame_is_already_sufficient():
    full = _central_detail_frame()
    unchanged = crop_capture(
        full,
        NormalizedBBox(left=0.0, top=0.0, right=1.0, bottom=1.0),
    )
    assert unchanged is full


def test_26_visual_answer_wording_and_process_identity_are_not_reproved():
    strategy, _contract_value = _contract(
        "What is the background theme on the MSI Center window?"
    )
    # The mechanical receipt deliberately carries a different process-style
    # identity. Runtime authority is image delivery, not string equivalence.
    _record_image(strategy)
    postcondition = strategy.required_postconditions[0]

    proven = strategy.prove_visual_answer(
        "MSI Center has a dark blue/purple space-themed background.",
        evidence="terra-answer",
        provider="chatgpt_account",
        model="gpt-5.6-terra",
    )

    assert proven == (postcondition,)
    assert not strategy._latest_desktop_observation.get(
        "visual_answer_rejection"
    )
