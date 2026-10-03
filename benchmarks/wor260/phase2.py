"""Executable in-memory WOR-260 Phase-2 shadow benchmark.

The scenarios contain only immutable fixtures.  Running this module performs
no production discovery, provider call, browser/desktop action, persistence,
permission grant, or terminality transition.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from statistics import fmean

from benchmarks.wor260.schema import TARGET_COVERAGE_MATRIX
from osia.adaptive_planner import (
    DivergenceClassification,
    LegacyDecisionSnapshot,
    PlannerLimits,
    ShadowPlanResult,
    plan_shadow,
)
from osia.capability_adapters import (
    AdaptiveCapabilityDescriptor,
    CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
    CapabilityFamily,
    DeterministicConstraint,
    ModalityRequirement,
    PermissionRequirement,
    Reversibility,
    RuntimeAvailability,
    UNKNOWN_METADATA,
    VerifierStrength,
)
from osia.capability_search import (
    AttemptEffectState,
    CapabilityPermissionState,
    DeterministicRoute,
    PermissionState,
    ShadowPlanningContext,
    StaleReference,
    TypedObservation,
)
from osia.goal_contract import (
    GOAL_CONTRACT_SCHEMA_VERSION,
    GoalContract,
    GoalExecutionBinding,
    GoalNodeContract,
    NOT_SPECIFIED,
    ObservablePostcondition,
    validate_goal_contract,
)
from osia.tool_contracts import SideEffectClass


@dataclass(frozen=True)
class ShadowBenchmarkExpectation:
    plan_found: bool
    first_capability_id: str = ""
    required_roles: tuple[str, ...] = ()
    hard_rejected_capability_ids: tuple[str, ...] = ()
    forbidden_step_capability_ids: tuple[str, ...] = ()
    full_scan_required: bool = False
    requires_exact_verification: bool = False


@dataclass(frozen=True)
class ShadowBenchmarkScenario:
    scenario_id: str
    categories: tuple[str, ...]
    context: ShadowPlanningContext
    expectation: ShadowBenchmarkExpectation
    legacy_snapshot: LegacyDecisionSnapshot | None
    limits: PlannerLimits = PlannerLimits()


@dataclass(frozen=True)
class ShadowBenchmarkOutcome:
    scenario_id: str
    categories: tuple[str, ...]
    result: ShadowPlanResult
    semantic_fidelity: bool
    false_success: bool
    retrieval_false_negative: bool


@dataclass(frozen=True)
class ShadowBenchmarkMetrics:
    scenario_count: int
    plan_found_rate: float
    semantic_fidelity_rate: float
    false_success_rate: float
    hard_constraint_violation_count: int
    permission_violation_count: int
    identity_violation_count: int
    outcome_unknown_replay_violation_count: int
    modality_violation_count: int
    retrieval_false_negative_count: int
    full_scan_recovery_count: int
    average_initial_candidates: float
    average_eligible_candidates: float
    average_plan_steps: float
    legacy_shadow_agreement_rate: float
    plausibly_better_count: int
    semantically_equivalent_divergence_count: int
    worse_count: int
    unresolved_count: int
    impossible_to_compare_count: int


@dataclass(frozen=True)
class ShadowBenchmarkReport:
    target_scenario_count: int
    outcomes: tuple[ShadowBenchmarkOutcome, ...]
    metrics: ShadowBenchmarkMetrics
    plausibly_better_scenario_ids: tuple[str, ...]
    semantically_equivalent_divergence_scenario_ids: tuple[str, ...]
    worse_scenario_ids: tuple[str, ...]
    unresolved_scenario_ids: tuple[str, ...]
    impossible_to_compare_scenario_ids: tuple[str, ...]


def _fingerprint(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def fixture_node(
    node_id: str,
    objective: str,
    target: str,
    application: str,
    surface: str,
    *,
    account: str = NOT_SPECIFIED,
    side_effect: str = SideEffectClass.READ_ONLY.value,
    postcondition: str = "",
) -> GoalNodeContract:
    postconditions = (
        (ObservablePostcondition(
            description=postcondition,
            target=target,
            application=application,
            surface=surface,
            account_profile=account,
            source="phase2_benchmark_fixture",
        ),)
        if postcondition else ()
    )
    return GoalNodeContract(
        node_id=node_id,
        objective=objective,
        target=target,
        application=application,
        required_surface=surface,
        required_account_profile=account,
        constraints=(),
        desired_state=(),
        forbidden_false_success_states=(),
        observable_postconditions=postconditions,
        side_effect_class=side_effect,
        read_only=side_effect == SideEffectClass.READ_ONLY.value,
        mutation=side_effect != SideEffectClass.READ_ONLY.value,
        source_semantic_node_fingerprint=f"fixture:{node_id}",
    )


def fixture_goal_contract(
    *nodes: GoalNodeContract,
    root_objective: str | None = None,
) -> GoalContract:
    provisional = GoalContract(
        schema_version=GOAL_CONTRACT_SCHEMA_VERSION,
        root_objective=root_objective or " then ".join(
            item.objective for item in nodes
        ),
        legacy_fingerprint="phase2_fixture",
        nodes=tuple(nodes),
        global_constraints=(),
        execution_binding=GoalExecutionBinding(),
        fingerprint="",
    )
    contract = replace(
        provisional,
        fingerprint=_fingerprint(provisional.semantic_dict()),
    )
    return validate_goal_contract(contract)


def fixture_descriptor(
    capability_id: str,
    family: str,
    role: str,
    *,
    target: str = UNKNOWN_METADATA,
    application: str = UNKNOWN_METADATA,
    surface: str = UNKNOWN_METADATA,
    account: str = UNKNOWN_METADATA,
    effect: str = "explicit benchmark effect",
    availability: str = RuntimeAvailability.AVAILABLE.value,
    permission_category: str = "none",
    confirmation_policy: str = "no_confirmation",
    verifier: str = VerifierStrength.EXACT_POSTCONDITION.value,
    side_effect: str = SideEffectClass.READ_ONLY.value,
    reversibility: str | None = None,
    modalities: tuple[str, ...] = (),
    equivalence_class: str = UNKNOWN_METADATA,
    supported_surface: str | None = None,
    exact_postcondition: str = "",
    description: str | None = None,
    extra_constraints: tuple[tuple[str, str], ...] = (),
    latency_class: str = UNKNOWN_METADATA,
) -> AdaptiveCapabilityDescriptor:
    constraints = [
        DeterministicConstraint("step_role", role, "phase2_benchmark_fixture"),
    ]
    for name, value in (
        ("target", target),
        ("application", application),
        ("surface", surface),
        ("account_profile", account),
        ("exact_postcondition", exact_postcondition),
    ):
        if value not in {"", UNKNOWN_METADATA, NOT_SPECIFIED}:
            constraints.append(DeterministicConstraint(
                name, value, "phase2_benchmark_fixture"
            ))
    constraints.extend(
        DeterministicConstraint(name, value, "phase2_benchmark_fixture")
        for name, value in extra_constraints
    )
    if reversibility is None:
        reversibility = (
            Reversibility.NOT_APPLICABLE.value
            if side_effect == SideEffectClass.READ_ONLY.value
            else Reversibility.REVERSIBLE.value
            if side_effect == SideEffectClass.REVERSIBLE_LOCAL.value
            else Reversibility.UNKNOWN.value
        )
    return AdaptiveCapabilityDescriptor(
        schema_version=CAPABILITY_DESCRIPTOR_SCHEMA_VERSION,
        capability_id=capability_id,
        family=family,
        semantic_inputs=(),
        semantic_effects=(effect,),
        preconditions=(),
        permission_requirements=(PermissionRequirement(
            permission_category, confirmation_policy
        ),),
        supported_surfaces=(supported_surface or surface,),
        required_account_profile=account,
        modality_requirements=modalities,
        runtime_availability=availability,
        verifier_strength=verifier,
        side_effect_class=side_effect,
        reversibility=reversibility,
        anti_replay_equivalence_class=equivalence_class,
        deterministic_constraints=tuple(constraints),
        source_authority="phase2_benchmark_fixture",
        description=description or effect,
        latency_class=latency_class,
    )


def _context(
    contract: GoalContract,
    capabilities: tuple[AdaptiveCapabilityDescriptor, ...],
    **changes: object,
) -> ShadowPlanningContext:
    base = ShadowPlanningContext(
        goal_contract=contract,
        active_goal_node_id=contract.nodes[0].node_id,
        capabilities=capabilities,
    )
    return replace(base, **changes)


def _legacy(
    capability_id: str,
    *,
    family: str = UNKNOWN_METADATA,
    step_count: int = 1,
    verifier: str = VerifierStrength.EXACT_POSTCONDITION.value,
    semantic_plan: tuple[str, ...] = (),
    frontier: tuple[str, ...] = (),
) -> LegacyDecisionSnapshot:
    return LegacyDecisionSnapshot(
        first_mechanism_id=capability_id,
        first_family=family,
        semantic_plan=semantic_plan or (capability_id,),
        step_count=step_count,
        verifier_strength=verifier,
        frontier_capability_ids=frontier,
    )


def _single_scenario(
    scenario_id: str,
    categories: tuple[str, ...],
    node: GoalNodeContract,
    capabilities: tuple[AdaptiveCapabilityDescriptor, ...],
    expectation: ShadowBenchmarkExpectation,
    *,
    legacy: LegacyDecisionSnapshot | None = None,
    limits: PlannerLimits = PlannerLimits(),
    context_changes: dict[str, object] | None = None,
) -> ShadowBenchmarkScenario:
    contract = fixture_goal_contract(node)
    return ShadowBenchmarkScenario(
        scenario_id=scenario_id,
        categories=categories,
        context=_context(contract, capabilities, **(context_changes or {})),
        expectation=expectation,
        legacy_snapshot=legacy,
        limits=limits,
    )


def benchmark_scenarios() -> tuple[ShadowBenchmarkScenario, ...]:
    """Return 41 distinct high-signal executable Phase-2 scenarios."""

    read = SideEffectClass.READ_ONLY.value
    local = SideEffectClass.REVERSIBLE_LOCAL.value
    external = SideEffectClass.EXTERNAL_MUTATION.value
    direct = CapabilityFamily.DIRECT_API.value
    browser = CapabilityFamily.BROWSER_DOM.value
    uia = CapabilityFamily.DESKTOP_UIA.value
    scenarios: list[ShadowBenchmarkScenario] = []

    # Direct/API/MCP/provider-native selection.
    node = fixture_node("n1", "Search invoice 42", "invoice 42", "account", "account_data", postcondition="invoice 42 is returned")
    api = fixture_descriptor("api:invoice_42", direct, "act", target="invoice 42", application="account", surface="account_data", effect="search invoice 42", exact_postcondition="invoice 42 is returned")
    ui = fixture_descriptor("browser:invoice_42", browser, "act", target="invoice 42", application="account", surface="account_data", effect="search invoice 42", verifier=VerifierStrength.STRUCTURED_RESULT.value)
    verify = fixture_descriptor("verify:invoice_42", direct, "verify", target="invoice 42", application="account", surface="account_data", effect="verify invoice 42", verifier=VerifierStrength.EXACT_POSTCONDITION.value)
    scenarios.append(_single_scenario("direct_api_exact_vs_ui", ("direct_api", "ranking"), node, (api, ui, verify), ShadowBenchmarkExpectation(True, "api:invoice_42", ("act",)), legacy=_legacy("browser:invoice_42", step_count=2, verifier=VerifierStrength.STRUCTURED_RESULT.value)))

    node = fixture_node("n1", "Search CRM contact Ada", "Ada", "crm", "records", postcondition="Ada contact is returned")
    mcp = fixture_descriptor("mcp:crm:search_ada", CapabilityFamily.MCP.value, "act", target="Ada", application="crm", surface="records", effect="search CRM Ada", exact_postcondition="Ada contact is returned")
    web = fixture_descriptor("browser:crm_ada", browser, "act", target="Ada", application="crm", surface="records", effect="search CRM Ada", verifier=VerifierStrength.WEAK_UNSTRUCTURED.value)
    scenarios.append(_single_scenario("exact_mcp_vs_browser", ("mcp", "ranking"), node, (mcp, web), ShadowBenchmarkExpectation(True, mcp.capability_id), legacy=_legacy(web.capability_id, step_count=3, verifier=VerifierStrength.WEAK_UNSTRUCTURED.value)))

    node = fixture_node("n1", "Search the web for OSIA", "OSIA", "provider", "web", postcondition="OSIA results are returned")
    native = fixture_descriptor("webSearch", CapabilityFamily.PROVIDER_NATIVE.value, "act", target="OSIA", application="provider", surface="web", effect="search web OSIA", exact_postcondition="OSIA results are returned")
    dom = fixture_descriptor("browser:web_search", browser, "act", target="OSIA", application="provider", surface="web", effect="search web OSIA", verifier=VerifierStrength.OBSERVABLE_STATE.value)
    scenarios.append(_single_scenario("provider_native_not_artificially_demoted", ("provider_native", "ranking"), node, (native, dom), ShadowBenchmarkExpectation(True, native.capability_id), legacy=_legacy(dom.capability_id, semantic_plan=(native.capability_id,))))

    node = fixture_node("n1", "Search catalog item 7", "item 7", "catalog", "catalog", postcondition="item 7 is shown")
    unavailable = fixture_descriptor("api:item_7", direct, "act", target="item 7", application="catalog", surface="catalog", effect="search item 7", availability=RuntimeAvailability.UNAVAILABLE.value, exact_postcondition="item 7 is shown")
    fallback = fixture_descriptor("browser:item_7", browser, "act", target="item 7", application="catalog", surface="catalog", effect="search item 7", exact_postcondition="item 7 is shown")
    scenarios.append(_single_scenario("direct_unavailable_fallback", ("direct_api", "availability"), node, (unavailable, fallback), ShadowBenchmarkExpectation(True, fallback.capability_id, hard_rejected_capability_ids=(unavailable.capability_id,)), legacy=_legacy(unavailable.capability_id)))

    node = fixture_node("n1", "Read invoice in work account", "invoice", "billing", "records", account="work", postcondition="work invoice is visible")
    wrong = fixture_descriptor("api:personal_invoice", direct, "act", target="invoice", application="billing", surface="records", account="personal", effect="read invoice", exact_postcondition="work invoice is visible")
    correct = fixture_descriptor("browser:work_invoice", browser, "act", target="invoice", application="billing", surface="records", account="work", effect="read invoice", exact_postcondition="work invoice is visible")
    scenarios.append(_single_scenario("wrong_account_direct_rejected", ("identity", "direct_api"), node, (wrong, correct), ShadowBenchmarkExpectation(True, correct.capability_id, hard_rejected_capability_ids=(wrong.capability_id,)), legacy=_legacy(wrong.capability_id)))

    node = fixture_node("n1", "Read report alpha", "report alpha", "reports", "records", postcondition="report alpha is returned")
    unknown = fixture_descriptor("api:report_alpha_unknown", direct, "act", target="report alpha", application="reports", surface="records", effect="read report alpha", availability=RuntimeAvailability.UNKNOWN.value, exact_postcondition="report alpha is returned")
    available = fixture_descriptor("mcp:report_alpha_available", CapabilityFamily.MCP.value, "act", target="report alpha", application="reports", surface="records", effect="read report alpha", exact_postcondition="report alpha is returned")
    scenarios.append(_single_scenario("unknown_availability_vs_proven_available", ("availability", "ranking"), node, (unknown, available), ShadowBenchmarkExpectation(True, available.capability_id), legacy=_legacy(available.capability_id)))

    node = fixture_node("n1", "Read discovered archive record", "archive record", "archive", "records", postcondition="archive record is returned")
    discover = fixture_descriptor("discover:archive_route", CapabilityFamily.OSIA_NATIVE.value, "discover", target="archive record", application="archive", surface="records", effect="discover archive capability availability", verifier=VerifierStrength.OBSERVABLE_STATE.value)
    frontier = fixture_descriptor("api:archive_frontier", direct, "act", target="archive record", application="archive", surface="records", effect="read discovered archive record", availability=RuntimeAvailability.UNKNOWN.value, exact_postcondition="archive record is returned")
    scenarios.append(_single_scenario("discover_unresolved_runtime_frontier", ("retrieval", "discover"), node, (discover, frontier), ShadowBenchmarkExpectation(True, frontier.capability_id, ("discover", "act")), legacy=_legacy(frontier.capability_id)))

    # Surface fidelity.
    node = fixture_node("n1", "Search Subnautica inside Steam Library", "Subnautica", "Steam", "Steam Library", postcondition="Subnautica appears in Steam Library")
    store = fixture_descriptor("browser:steam_store_search", browser, "act", target="Subnautica", application="Steam", surface="Steam Store", effect="search Subnautica", exact_postcondition="Subnautica appears in Steam Library")
    library = fixture_descriptor("uia:steam_library_search", uia, "act", target="Subnautica", application="Steam", surface="Steam Library", effect="search Subnautica Steam Library", exact_postcondition="Subnautica appears in Steam Library")
    scenarios.append(_single_scenario("steam_library_vs_store", ("surface_fidelity",), node, (store, library), ShadowBenchmarkExpectation(True, library.capability_id, hard_rejected_capability_ids=(store.capability_id,)), legacy=_legacy(store.capability_id)))

    node = fixture_node("n1", "Open the native Settings Bluetooth panel", "Bluetooth", "Settings", "native_settings", postcondition="native Bluetooth panel is open")
    web_settings = fixture_descriptor("browser:settings_help", browser, "act", target="Bluetooth", application="Settings", surface="web", effect="open Bluetooth help", exact_postcondition="native Bluetooth panel is open")
    native_settings = fixture_descriptor("route:native_bluetooth", CapabilityFamily.DEEP_LINK.value, "act", target="Bluetooth", application="Settings", surface="native_settings", effect="open native Bluetooth panel", exact_postcondition="native Bluetooth panel is open")
    scenarios.append(_single_scenario("native_required_vs_web_surface", ("surface_fidelity", "deep_link"), node, (web_settings, native_settings), ShadowBenchmarkExpectation(True, native_settings.capability_id, hard_rejected_capability_ids=(web_settings.capability_id,)), legacy=_legacy(web_settings.capability_id)))

    # Desktop routing, observation, accessibility, and stale references.
    node = fixture_node("n1", "Activate exact Bluetooth control", "Bluetooth control", "Settings", "native_settings", side_effect=local, postcondition="Bluetooth control is active")
    exact_uia = fixture_descriptor("uia:bluetooth_exact", uia, "act", target="Bluetooth control", application="Settings", surface="native_settings", effect="activate exact Bluetooth control", side_effect=local, exact_postcondition="Bluetooth control is active", modalities=(ModalityRequirement.ACCESSIBILITY.value,))
    scenarios.append(_single_scenario("desktop_uia_rich_exact_target", ("desktop", "uia"), node, (exact_uia,), ShadowBenchmarkExpectation(True, exact_uia.capability_id), legacy=_legacy(exact_uia.capability_id), context_changes={"accessibility_quality": "rich_target_resolved"}))

    observe = fixture_descriptor("vision:observe_control", CapabilityFamily.VISION_OBSERVATION.value, "observe", target="Bluetooth control", application="Settings", surface="native_settings", effect="observe local control", verifier=VerifierStrength.OBSERVABLE_STATE.value, modalities=(ModalityRequirement.VISION.value,))
    resolve = fixture_descriptor("uia:resolve_control", uia, "resolve", target="Bluetooth control", application="Settings", surface="native_settings", effect="resolve exact UIA target", verifier=VerifierStrength.OBSERVABLE_STATE.value, modalities=(ModalityRequirement.ACCESSIBILITY.value,))
    act = fixture_descriptor("uia:act_control", uia, "act", target="Bluetooth control", application="Settings", surface="native_settings", effect="activate Bluetooth control", side_effect=local, verifier=VerifierStrength.STRUCTURED_RESULT.value, modalities=(ModalityRequirement.ACCESSIBILITY.value,))
    verifier = fixture_descriptor("uia:verify_control", uia, "verify", target="Bluetooth control", application="Settings", surface="native_settings", effect="verify Bluetooth control active", verifier=VerifierStrength.EXACT_POSTCONDITION.value, exact_postcondition="Bluetooth control is active", modalities=(ModalityRequirement.ACCESSIBILITY.value,))
    vision_context = {"accessibility_quality": "target_missing", "authoritative_vision_available": True, "effective_model_modalities": ("text", "image")}
    scenarios.append(_single_scenario("target_missing_with_vision", ("desktop", "vision"), node, (observe, resolve, act, verifier), ShadowBenchmarkExpectation(True, act.capability_id, ("observe", "resolve", "act", "verify")), legacy=_legacy(act.capability_id, step_count=1, verifier=VerifierStrength.STRUCTURED_RESULT.value), context_changes=vision_context))
    nonvision_context = dict(vision_context)
    nonvision_context.update({"authoritative_vision_available": False, "effective_model_modalities": ("text",)})
    scenarios.append(_single_scenario("target_missing_nonvision", ("desktop", "vision"), node, (observe, resolve, act, verifier), ShadowBenchmarkExpectation(False, hard_rejected_capability_ids=(observe.capability_id,), full_scan_required=True), legacy=_legacy(act.capability_id), context_changes=nonvision_context))
    scenarios.append(_single_scenario("vision_observation_then_uia_action", ("desktop", "vision", "uia"), node, (act, verifier, observe, resolve), ShadowBenchmarkExpectation(True, act.capability_id, ("observe", "resolve", "act", "verify")), legacy=_legacy("vision:global_click", step_count=1), context_changes=vision_context))

    visual_act = fixture_descriptor("vision:local_act_after_observe", CapabilityFamily.VISION_ACTUATION_LOCAL.value, "act", target="Bluetooth control", application="Settings", surface="native_settings", effect="activate observed local control", side_effect=local, exact_postcondition="Bluetooth control is active", modalities=(ModalityRequirement.VISION.value, ModalityRequirement.LOCAL_REGION.value,), extra_constraints=(("requires_observation", "true"),))
    scenarios.append(_single_scenario("observe_then_act", ("desktop", "vision", "observe"), node, (observe, visual_act), ShadowBenchmarkExpectation(True, visual_act.capability_id, ("observe", "act")), legacy=_legacy(visual_act.capability_id), context_changes={"authoritative_vision_available": True, "effective_model_modalities": ("text", "image")}))

    node_keyboard = fixture_node("n1", "Activate Save with keyboard", "Save", "Editor", "native_editor", side_effect=local, postcondition="Save action is observed")
    keyboard = fixture_descriptor("keyboard:save", CapabilityFamily.DESKTOP_KEYBOARD.value, "act", target="Save", application="Editor", surface="native_editor", effect="activate Save with keyboard", side_effect=local, exact_postcondition="Save action is observed", modalities=(ModalityRequirement.KEYBOARD_WITH_OBSERVABLE_FOCUS.value,))
    scenarios.append(_single_scenario("keyboard_when_focus_observable", ("desktop", "keyboard"), node_keyboard, (keyboard,), ShadowBenchmarkExpectation(True, keyboard.capability_id), legacy=_legacy(keyboard.capability_id), context_changes={"typed_observations": (TypedObservation("focus", "save_button", "fixture"),)}))

    node_route = fixture_node("n1", "Open exact project route", "project 42", "Editor", "native_editor", side_effect=local, postcondition="project 42 is open")
    route = fixture_descriptor("route:project_42", CapabilityFamily.DEEP_LINK.value, "act", target="project 42", application="Editor", surface="native_editor", effect="open exact project route", side_effect=local, exact_postcondition="project 42 is open")
    ui_steps = fixture_descriptor("uia:open_project_42", uia, "act", target="project 42", application="Editor", surface="native_editor", effect="open project 42 via UI", side_effect=local, exact_postcondition="project 42 is open")
    scenarios.append(_single_scenario("exact_deep_link", ("desktop", "deep_link"), node_route, (ui_steps, route), ShadowBenchmarkExpectation(True, route.capability_id), legacy=_legacy(ui_steps.capability_id, step_count=3, verifier=VerifierStrength.OBSERVABLE_STATE.value)))

    global_click = fixture_descriptor("vision:global_project_click", CapabilityFamily.VISION_ACTUATION_GLOBAL.value, "act", target="project 42", application="Editor", surface="native_editor", effect="global coordinate project click", side_effect=local, exact_postcondition="project 42 is open", modalities=(ModalityRequirement.VISION.value, ModalityRequirement.GLOBAL_COORDINATES.value,), latency_class="low")
    scenarios.append(_single_scenario("global_coordinates_last_resort", ("desktop", "vision", "ranking"), node_route, (global_click, route), ShadowBenchmarkExpectation(True, route.capability_id), legacy=_legacy(global_click.capability_id), context_changes={"authoritative_vision_available": True, "effective_model_modalities": ("text", "image")}))

    stale_node = fixture_node("n1", "Activate current Save control", "Save", "Editor", "native_editor", side_effect=local, postcondition="Save control is active")
    stale_act = fixture_descriptor("uia:stale_save", uia, "act", target="Save", application="Editor", surface="native_editor", effect="activate Save", side_effect=local, exact_postcondition="Save control is active")
    stale_resolve = fixture_descriptor("uia:resolve_save", uia, "resolve", target="Save", application="Editor", surface="native_editor", effect="resolve current Save")
    scenarios.append(_single_scenario("desktop_stale_ref_resolve", ("stale_ref", "desktop"), stale_node, (stale_act, stale_resolve), ShadowBenchmarkExpectation(True, stale_act.capability_id, ("resolve", "act")), legacy=_legacy(stale_act.capability_id), context_changes={"stale_references": (StaleReference(stale_act.capability_id, "old-save", stale_resolve.capability_id),)}))

    browser_act = fixture_descriptor("browser:stale_submit", browser, "act", target="Submit", application="Browser", surface="form", effect="submit form", side_effect=external, exact_postcondition="form submitted", equivalence_class="submit_form")
    browser_resolve = fixture_descriptor("browser:resolve_submit", browser, "resolve", target="Submit", application="Browser", surface="form", effect="resolve current DOM Submit")
    browser_node = fixture_node("n1", "Submit current form", "Submit", "Browser", "form", side_effect=external, postcondition="form submitted")
    permitted_browser = {"permission_states": (CapabilityPermissionState(browser_act.capability_id, PermissionState.PERMITTED.value),), "stale_references": (StaleReference(browser_act.capability_id, "old-dom", browser_resolve.capability_id),)}
    scenarios.append(_single_scenario("browser_stale_ref_resolve", ("stale_ref", "browser"), browser_node, (browser_act, browser_resolve), ShadowBenchmarkExpectation(True, browser_act.capability_id, ("resolve", "act")), legacy=_legacy(browser_act.capability_id), context_changes=permitted_browser))
    scenarios.append(_single_scenario("stale_ref_no_direct_actuation", ("stale_ref", "security"), stale_node, (stale_act,), ShadowBenchmarkExpectation(False, hard_rejected_capability_ids=(stale_act.capability_id,), full_scan_required=True), legacy=_legacy(stale_act.capability_id), context_changes={"stale_references": (StaleReference(stale_act.capability_id, "old-save"),)}))

    # outcome_unknown and permission gates.
    send_node = fixture_node("n1", "Send prepared message", "prepared message", "mail", "outbox", side_effect=external, postcondition="message is confirmed sent")
    send = fixture_descriptor("api:send_message", direct, "act", target="prepared message", application="mail", surface="outbox", effect="send prepared message", side_effect=external, equivalence_class="send_message", exact_postcondition="message is confirmed sent")
    reconcile = fixture_descriptor("api:reconcile_message", direct, "reconcile", target="prepared message", application="mail", surface="outbox", effect="read sent-message state", verifier=VerifierStrength.EXACT_POSTCONDITION.value)
    unknown_effect = (AttemptEffectState(send.capability_id, "send_message", "outcome_unknown"),)
    permitted_send = (CapabilityPermissionState(send.capability_id, PermissionState.PERMITTED.value),)
    scenarios.append(_single_scenario("outcome_unknown_blocks_mutation_replay", ("outcome_unknown", "security"), send_node, (send, reconcile), ShadowBenchmarkExpectation(True, reconcile.capability_id, ("reconcile",), hard_rejected_capability_ids=(send.capability_id,), forbidden_step_capability_ids=(send.capability_id,)), legacy=_legacy(send.capability_id), context_changes={"previous_attempt_effects": unknown_effect, "permission_states": permitted_send}))

    reconcile_node = fixture_node("n1", "Determine whether message was sent", "prepared message", "mail", "outbox", postcondition="sent state is observed")
    read_reconcile = fixture_descriptor("api:read_sent_state", direct, "reconcile", target="prepared message", application="mail", surface="outbox", effect="read sent state", verifier=VerifierStrength.EXACT_POSTCONDITION.value, exact_postcondition="sent state is observed")
    scenarios.append(_single_scenario("read_only_reconciliation_preferred", ("outcome_unknown", "reconciliation"), reconcile_node, (send, read_reconcile), ShadowBenchmarkExpectation(True, read_reconcile.capability_id, ("reconcile",), forbidden_step_capability_ids=(send.capability_id,)), legacy=_legacy(send.capability_id), context_changes={"previous_attempt_effects": unknown_effect, "permission_states": permitted_send}))

    confirmed_failure = (AttemptEffectState(send.capability_id, "send_message", "confirmed_failure_before_effect"),)
    scenarios.append(_single_scenario("safe_after_confirmed_failure_before_effect", ("outcome_unknown", "replay"), send_node, (send,), ShadowBenchmarkExpectation(True, send.capability_id), legacy=_legacy(send.capability_id), context_changes={"previous_attempt_effects": confirmed_failure, "permission_states": permitted_send}))

    scenarios.append(_single_scenario("permission_permitted", ("permissions",), send_node, (send,), ShadowBenchmarkExpectation(True, send.capability_id), legacy=_legacy(send.capability_id), context_changes={"permission_states": permitted_send}))
    scenarios.append(_single_scenario("permission_required", ("permissions",), send_node, (send,), ShadowBenchmarkExpectation(True, send.capability_id), legacy=_legacy(send.capability_id), context_changes={"permission_states": (CapabilityPermissionState(send.capability_id, PermissionState.PERMISSION_REQUIRED.value),)}))
    scenarios.append(_single_scenario("permission_disallowed", ("permissions", "security"), send_node, (send,), ShadowBenchmarkExpectation(False, hard_rejected_capability_ids=(send.capability_id,), full_scan_required=True), legacy=_legacy(send.capability_id), context_changes={"permission_states": (CapabilityPermissionState(send.capability_id, PermissionState.DISALLOWED.value),)}))
    scenarios.append(_single_scenario("permission_unknown", ("permissions", "security"), send_node, (send,), ShadowBenchmarkExpectation(True, send.capability_id), legacy=_legacy(send.capability_id), context_changes={"permission_states": (CapabilityPermissionState(send.capability_id, PermissionState.UNKNOWN.value),)}))

    # Multi-node planning, completion preservation, and ordering.
    read_node = fixture_node("n1", "Read approval status", "approval", "workflow", "records", postcondition="approval status is read")
    decide_node = fixture_node("n2", "Read policy decision", "policy", "workflow", "records", postcondition="policy decision is read")
    mutate_node = fixture_node("n3", "Apply approved change", "approved change", "workflow", "records", side_effect=external, postcondition="approved change is applied")
    read_cap = fixture_descriptor("api:read_approval", direct, "act", target="approval", application="workflow", surface="records", effect="read approval status", exact_postcondition="approval status is read")
    decide_cap = fixture_descriptor("api:read_policy", direct, "act", target="policy", application="workflow", surface="records", effect="read policy decision", exact_postcondition="policy decision is read")
    mutate_cap = fixture_descriptor("api:apply_change", direct, "act", target="approved change", application="workflow", surface="records", effect="apply approved change", side_effect=external, exact_postcondition="approved change is applied")
    multi_contract = fixture_goal_contract(read_node, decide_node, mutate_node)
    multi_context = _context(multi_contract, (read_cap, decide_cap, mutate_cap), permission_states=(CapabilityPermissionState(mutate_cap.capability_id, PermissionState.PERMITTED.value),))
    scenarios.append(ShadowBenchmarkScenario("read_decide_mutate", ("multi_step", "ordering"), multi_context, ShadowBenchmarkExpectation(True, read_cap.capability_id, ("act",)), _legacy(read_cap.capability_id, semantic_plan=(read_cap.capability_id, decide_cap.capability_id, mutate_cap.capability_id), step_count=3)))

    steam_node = fixture_node("n1", "Open Steam", "Steam", "Steam", "native", side_effect=local, postcondition="Steam is open")
    spotify_node = fixture_node("n2", "Open Spotify", "Spotify", "Spotify", "native", side_effect=local, postcondition="Spotify is open")
    steam_route = fixture_descriptor("route:steam", CapabilityFamily.DEEP_LINK.value, "act", target="Steam", application="Steam", surface="native", effect="open Steam", side_effect=local, exact_postcondition="Steam is open")
    spotify_route = fixture_descriptor("route:spotify", CapabilityFamily.DEEP_LINK.value, "act", target="Spotify", application="Spotify", surface="native", effect="open Spotify", side_effect=local, exact_postcondition="Spotify is open")
    app_contract = fixture_goal_contract(steam_node, spotify_node)
    app_context = _context(app_contract, (steam_route, spotify_route))
    scenarios.append(ShadowBenchmarkScenario("two_apps_sequential", ("multi_step", "two_apps"), app_context, ShadowBenchmarkExpectation(True, steam_route.capability_id), _legacy(steam_route.capability_id, semantic_plan=(steam_route.capability_id, spotify_route.capability_id), step_count=2)))
    completed_context = replace(app_context, completed_goal_node_ids=(steam_node.node_id,), active_goal_node_id=spotify_node.node_id)
    scenarios.append(ShadowBenchmarkScenario("completed_node_preserved", ("multi_step", "completion"), completed_context, ShadowBenchmarkExpectation(True, spotify_route.capability_id), _legacy(spotify_route.capability_id)))

    mut1 = fixture_node("n1", "Create draft", "draft", "workflow", "records", side_effect=external, postcondition="draft exists")
    mut2 = fixture_node("n2", "Publish draft", "draft", "workflow", "records", side_effect=external, postcondition="draft published")
    create = fixture_descriptor("api:create_draft", direct, "act", target="draft", application="workflow", surface="records", effect="create draft", side_effect=external, exact_postcondition="draft exists")
    publish = fixture_descriptor("api:publish_draft", direct, "act", target="draft", application="workflow", surface="records", effect="publish draft", side_effect=external, exact_postcondition="draft published")
    mutation_contract = fixture_goal_contract(mut1, mut2)
    mutation_context = _context(mutation_contract, (create, publish), permission_states=(CapabilityPermissionState("*", PermissionState.PERMITTED.value),))
    scenarios.append(ShadowBenchmarkScenario("mutation_ordering_preserved", ("multi_step", "ordering"), mutation_context, ShadowBenchmarkExpectation(True, create.capability_id), _legacy(create.capability_id, semantic_plan=(create.capability_id, publish.capability_id), step_count=2)))

    read_a = fixture_node("n1", "Read dashboard A", "dashboard A", "analytics", "records", postcondition="dashboard A read")
    read_b = fixture_node("n2", "Read dashboard B", "dashboard B", "analytics", "records", postcondition="dashboard B read")
    cap_a = fixture_descriptor("api:dashboard_a", direct, "act", target="dashboard A", application="analytics", surface="records", effect="read dashboard A", exact_postcondition="dashboard A read")
    cap_b = fixture_descriptor("api:dashboard_b", direct, "act", target="dashboard B", application="analytics", surface="records", effect="read dashboard B", exact_postcondition="dashboard B read")
    reads_contract = fixture_goal_contract(read_a, read_b)
    scenarios.append(ShadowBenchmarkScenario("independent_read_only_nodes", ("multi_step", "read_only"), _context(reads_contract, (cap_a, cap_b)), ShadowBenchmarkExpectation(True, cap_a.capability_id), _legacy(cap_a.capability_id, semantic_plan=(cap_a.capability_id, cap_b.capability_id), step_count=2)))
    scenarios.append(ShadowBenchmarkScenario("partial_success_preserved", ("multi_step", "partial_success"), _context(reads_contract, (cap_a, cap_b), completed_goal_node_ids=(read_a.node_id,), active_goal_node_id=read_b.node_id), ShadowBenchmarkExpectation(True, cap_b.capability_id), _legacy(cap_b.capability_id)))

    # Retrieval, full-scan recovery, and no-plan behavior.
    retrieval_node = fixture_node("n1", "Find exact lunar report", "lunar report", "archive", "records", postcondition="lunar report returned")
    exact = fixture_descriptor("api:lunar_report", direct, "act", target="lunar report", application="archive", surface="records", effect="find exact lunar report", exact_postcondition="lunar report returned")
    noises = tuple(
        fixture_descriptor(f"browser:lunar_noise_{index}", browser, "act", target=f"wrong {index}", application="archive", surface="records", effect="find lunar report noise", exact_postcondition="lunar report returned")
        for index in range(4)
    )
    scenarios.append(_single_scenario("retrieval_correct_top_k", ("retrieval", "top_k"), retrieval_node, (exact, *noises), ShadowBenchmarkExpectation(True, exact.capability_id), legacy=_legacy(exact.capability_id), limits=PlannerLimits(top_k=2, retrieval_limit=2)))

    opaque = fixture_descriptor("opaque:7f3a", direct, "act", target="sealed target", application="secure lab", surface="secure surface", supported_surface="desktop", effect="opaque operation", exact_postcondition="sealed target returned")
    full_node = fixture_node("n1", "Retrieve sealed target", "sealed target", "secure lab", "secure surface", postcondition="sealed target returned")
    full_noise = tuple(
        fixture_descriptor(f"noise:retrieve_sealed_{index}", browser, "act", target=f"other target {index}", application="secure lab", surface="web", effect="retrieve sealed target", exact_postcondition="sealed target returned")
        for index in range(3)
    )
    scenarios.append(_single_scenario("full_scan_recovery", ("retrieval", "full_scan"), full_node, (*full_noise, opaque), ShadowBenchmarkExpectation(True, opaque.capability_id, full_scan_required=True), legacy=_legacy("legacy:opaque", frontier=tuple(item.capability_id for item in full_noise)), limits=PlannerLimits(top_k=2, retrieval_limit=2)))

    forced = fixture_descriptor("route:opaque_deterministic", CapabilityFamily.DEEP_LINK.value, "act", target="sealed target", application="secure lab", surface="secure surface", supported_surface="desktop", effect="opaque route", side_effect=local, exact_postcondition="sealed target returned")
    forced_node = fixture_node("n1", "Open sealed target route", "sealed target", "secure lab", "secure surface", side_effect=local, postcondition="sealed target returned")
    scenarios.append(_single_scenario("deterministic_candidate_force_retained", ("retrieval", "deterministic_route"), forced_node, (*full_noise, forced), ShadowBenchmarkExpectation(True, forced.capability_id), legacy=_legacy(forced.capability_id), limits=PlannerLimits(top_k=2, retrieval_limit=1), context_changes={"deterministic_routes": (DeterministicRoute("route-1", forced.capability_id, "secure lab", "secure surface", "sealed target", True),)}))

    direct_down = fixture_descriptor("api:sealed_target", direct, "act", target="sealed target", application="secure lab", surface="secure surface", effect="retrieve sealed target", availability=RuntimeAvailability.UNAVAILABLE.value, exact_postcondition="sealed target returned")
    mcp_fallback = fixture_descriptor("mcp:sealed_target", CapabilityFamily.MCP.value, "act", target="sealed target", application="secure lab", surface="secure surface", effect="retrieve sealed target", exact_postcondition="sealed target returned")
    scenarios.append(_single_scenario("unavailable_direct_does_not_hide_fallback", ("retrieval", "availability"), full_node, (direct_down, mcp_fallback), ShadowBenchmarkExpectation(True, mcp_fallback.capability_id, hard_rejected_capability_ids=(direct_down.capability_id,)), legacy=_legacy(direct_down.capability_id)))

    wrong_only = fixture_descriptor("browser:wrong_surface_only", browser, "act", target="sealed target", application="secure lab", surface="public web", effect="retrieve sealed target", exact_postcondition="sealed target returned")
    scenarios.append(_single_scenario("no_plan_only_after_full_scan", ("retrieval", "no_plan"), full_node, (wrong_only,), ShadowBenchmarkExpectation(False, hard_rejected_capability_ids=(wrong_only.capability_id,), full_scan_required=True), legacy=_legacy(wrong_only.capability_id), limits=PlannerLimits(top_k=1, retrieval_limit=1)))

    # Verification strength: provider success is not an exact postcondition.
    verify_node = fixture_node("n1", "Update record title", "record title", "records", "record", side_effect=external, postcondition="record title equals final")
    provider_update = fixture_descriptor("api:update_title", direct, "act", target="record title", application="records", surface="record", effect="update record title", side_effect=external, verifier=VerifierStrength.PROVIDER_REPORTED.value, equivalence_class="update_title")
    exact_verify = fixture_descriptor("api:verify_title", direct, "verify", target="record title", application="records", surface="record", effect="read exact record title", verifier=VerifierStrength.EXACT_POSTCONDITION.value, exact_postcondition="record title equals final")
    verify_permissions = {"permission_states": (CapabilityPermissionState(provider_update.capability_id, PermissionState.PERMITTED.value),)}
    scenarios.append(_single_scenario("provider_success_needs_exact_verify", ("verification", "false_success"), verify_node, (provider_update, exact_verify), ShadowBenchmarkExpectation(True, provider_update.capability_id, ("act", "verify"), requires_exact_verification=True), legacy=_legacy(provider_update.capability_id, verifier=VerifierStrength.PROVIDER_REPORTED.value), context_changes=verify_permissions))

    weak_verify = fixture_descriptor("api:weak_verify_title", direct, "verify", target="record title", application="records", surface="record", effect="provider says updated", verifier=VerifierStrength.PROVIDER_REPORTED.value)
    observable_verify = fixture_descriptor("api:observable_verify_title", direct, "verify", target="record title", application="records", surface="record", effect="observe exact title", verifier=VerifierStrength.EXACT_POSTCONDITION.value, exact_postcondition="record title equals final")
    scenarios.append(_single_scenario("exact_postcondition_verifier_preferred", ("verification", "ranking"), verify_node, (provider_update, weak_verify, observable_verify), ShadowBenchmarkExpectation(True, provider_update.capability_id, ("act", "verify"), forbidden_step_capability_ids=(weak_verify.capability_id,), requires_exact_verification=True), legacy=_legacy(provider_update.capability_id, step_count=2, verifier=VerifierStrength.PROVIDER_REPORTED.value), context_changes=verify_permissions))

    assert 30 <= len(scenarios) <= 50
    return tuple(scenarios)


def _first_capability(result: ShadowPlanResult) -> str:
    if result.selected_plan is None:
        return ""
    for role in ("act", "reconcile"):
        step = next((
            item for item in result.selected_plan.ordered_steps
            if item.role == role
        ), None)
        if step is not None:
            return step.capability_id
    return (
        result.selected_plan.ordered_steps[0].capability_id
        if result.selected_plan.ordered_steps else ""
    )


def _grade_scenario(
    scenario: ShadowBenchmarkScenario,
    result: ShadowPlanResult,
) -> ShadowBenchmarkOutcome:
    expected = scenario.expectation
    plan_found = result.selected_plan is not None
    roles = {
        item.role for item in (
            result.selected_plan.ordered_steps if result.selected_plan else ()
        )
    }
    rejected = {
        item.capability_id
        for evidence in result.search_evidence
        for item in evidence.hard_rejected
    }
    steps = {
        item.capability_id for item in (
            result.selected_plan.ordered_steps if result.selected_plan else ()
        )
    }
    full_scan = any(item.full_scan_performed for item in result.search_evidence)
    semantic_fidelity = all((
        plan_found == expected.plan_found,
        not expected.first_capability_id
        or _first_capability(result) == expected.first_capability_id,
        set(expected.required_roles).issubset(roles),
        set(expected.hard_rejected_capability_ids).issubset(rejected),
        not set(expected.forbidden_step_capability_ids).intersection(steps),
        not expected.full_scan_required or full_scan,
    ))
    false_success = False
    if expected.requires_exact_verification and result.selected_plan is not None:
        candidates = {
            item.capability_id: item
            for evidence in result.search_evidence
            for item in (*evidence.eligible, *evidence.unresolved)
        }
        exact = any(
            candidates.get(item.capability_id) is not None
            and candidates[item.capability_id].verifier_strength == VerifierStrength.EXACT_POSTCONDITION.value
            for item in result.selected_plan.ordered_steps
        )
        false_success = not exact
    retrieval_false_negative = bool(
        expected.plan_found
        and not plan_found
        and result.search_evidence
        and all(item.full_scan_performed for item in result.search_evidence)
    )
    return ShadowBenchmarkOutcome(
        scenario_id=scenario.scenario_id,
        categories=scenario.categories,
        result=result,
        semantic_fidelity=semantic_fidelity,
        false_success=false_success,
        retrieval_false_negative=retrieval_false_negative,
    )


def run_shadow_benchmark(
    scenarios: tuple[ShadowBenchmarkScenario, ...] | None = None,
) -> ShadowBenchmarkReport:
    selected = scenarios or benchmark_scenarios()
    outcomes = tuple(
        _grade_scenario(
            scenario,
            plan_shadow(
                scenario.context,
                limits=scenario.limits,
                legacy_snapshot=scenario.legacy_snapshot,
            ),
        )
        for scenario in selected
    )
    count = len(outcomes)
    plans = tuple(
        item.result.selected_plan for item in outcomes
        if item.result.selected_plan is not None
    )
    all_evidence = tuple(
        evidence for item in outcomes for evidence in item.result.search_evidence
    )
    hard_violations = 0
    permission_violations = 0
    identity_violations = 0
    replay_violations = 0
    modality_violations = 0
    for outcome in outcomes:
        if outcome.result.selected_plan is None:
            continue
        selected_steps = {
            (item.goal_node_id, item.capability_id)
            for item in outcome.result.selected_plan.ordered_steps
        }
        unresolved = set(outcome.result.selected_plan.unresolved_constraints)
        for evidence in outcome.result.search_evidence:
            for rejected in evidence.hard_rejected:
                if (evidence.goal_node_id, rejected.capability_id) not in selected_steps:
                    continue
                hard_violations += 1
                reasons = set(rejected.hard_rejection_reasons)
                permission_violations += int("permission_disallowed" in reasons)
                identity_violations += int(bool(reasons.intersection({
                    "account_mismatch",
                })))
                replay_violations += int("outcome_unknown_replay_blocked" in reasons)
                modality_violations += int("modality_unavailable" in reasons)
            for candidate in evidence.unresolved:
                if (evidence.goal_node_id, candidate.capability_id) not in selected_steps:
                    continue
                if candidate.permission_state != PermissionState.PERMITTED.value:
                    expected_markers = {
                        f"{evidence.goal_node_id}:permission_required",
                        f"{evidence.goal_node_id}:permission_unknown",
                    }
                    if not unresolved.intersection(expected_markers):
                        permission_violations += 1

    comparisons = tuple(
        (item.scenario_id, item.result.legacy_comparison)
        for item in outcomes
    )
    agreeing = tuple(
        item for _, item in comparisons if item is not None and item.same_first_mechanism
    )
    comparable = tuple(item for _, item in comparisons if item is not None)

    def divergence_ids(classification: str, *, different_only: bool = False) -> tuple[str, ...]:
        return tuple(
            scenario_id for scenario_id, comparison in comparisons
            if comparison is not None
            and comparison.classification == classification
            and (not different_only or not comparison.same_first_mechanism)
        )

    better = divergence_ids(DivergenceClassification.PLAUSIBLY_BETTER.value)
    equivalent = divergence_ids(
        DivergenceClassification.SEMANTICALLY_EQUIVALENT.value,
        different_only=True,
    )
    worse = divergence_ids(DivergenceClassification.WORSE.value)
    unresolved_ids = divergence_ids(DivergenceClassification.UNRESOLVED.value)
    impossible = tuple(
        scenario_id for scenario_id, comparison in comparisons
        if comparison is None
        or comparison.classification == DivergenceClassification.IMPOSSIBLE_TO_COMPARE.value
    )
    metrics = ShadowBenchmarkMetrics(
        scenario_count=count,
        plan_found_rate=sum(
            item.result.selected_plan is not None for item in outcomes
        ) / count,
        semantic_fidelity_rate=sum(item.semantic_fidelity for item in outcomes) / count,
        false_success_rate=sum(item.false_success for item in outcomes) / count,
        hard_constraint_violation_count=hard_violations,
        permission_violation_count=permission_violations,
        identity_violation_count=identity_violations,
        outcome_unknown_replay_violation_count=replay_violations,
        modality_violation_count=modality_violations,
        retrieval_false_negative_count=sum(
            item.retrieval_false_negative for item in outcomes
        ),
        full_scan_recovery_count=sum(
            bool(evidence.full_scan_recovered_ids) for evidence in all_evidence
        ),
        average_initial_candidates=(
            fmean(len(item.initially_retrieved) for item in all_evidence)
            if all_evidence else 0.0
        ),
        average_eligible_candidates=(
            fmean(len(item.eligible) for item in all_evidence)
            if all_evidence else 0.0
        ),
        average_plan_steps=(
            fmean(len(item.ordered_steps) for item in plans) if plans else 0.0
        ),
        legacy_shadow_agreement_rate=(
            len(agreeing) / len(comparable) if comparable else 0.0
        ),
        plausibly_better_count=len(better),
        semantically_equivalent_divergence_count=len(equivalent),
        worse_count=len(worse),
        unresolved_count=len(unresolved_ids),
        impossible_to_compare_count=len(impossible),
    )
    return ShadowBenchmarkReport(
        target_scenario_count=sum(item.target_count for item in TARGET_COVERAGE_MATRIX),
        outcomes=outcomes,
        metrics=metrics,
        plausibly_better_scenario_ids=better,
        semantically_equivalent_divergence_scenario_ids=equivalent,
        worse_scenario_ids=worse,
        unresolved_scenario_ids=unresolved_ids,
        impossible_to_compare_scenario_ids=impossible,
    )


__all__ = [
    "ShadowBenchmarkExpectation",
    "ShadowBenchmarkMetrics",
    "ShadowBenchmarkOutcome",
    "ShadowBenchmarkReport",
    "ShadowBenchmarkScenario",
    "benchmark_scenarios",
    "fixture_descriptor",
    "fixture_goal_contract",
    "fixture_node",
    "run_shadow_benchmark",
]
