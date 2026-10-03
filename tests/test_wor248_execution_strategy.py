from __future__ import annotations

from osia.execution_strategy import (
    ExecutionStrategy,
    StrategyLayer,
    StrategyMechanism,
    build_execution_strategy,
    derive_semantic_goal,
    record_tool_observation,
    record_tool_outcome,
    tool_preflight,
    execution_strategy_scope,
)


def _mechanism(
    mechanism_id,
    layer,
    *,
    app="steam",
    surface="library",
    account="user_account",
    covers=True,
    mutation=True,
):
    return StrategyMechanism(
        mechanism_id,
        layer,
        f"controlled {mechanism_id}",
        application=app,
        surface=surface,
        account=account,
        covers_goal=covers,
        mutation=mutation,
    )


def _steam_goal():
    return derive_semantic_goal(
        "Open Steam, go to my Library and search for Subnautica"
    )


def _select(strategy, mechanism, action=None):
    return strategy.select(
        mechanism,
        action or {"target": "Subnautica"},
    )


def test_semantic_goal_contract_preserves_steam_library_invariants():
    goal = _steam_goal()

    assert goal.target_object == "subnautica"
    assert goal.required_application == "steam"
    assert goal.required_surface == "library"
    assert goal.required_account == "user_account"
    assert goal.requested_action == "search"
    assert "subnautica" in goal.observable_success_postcondition.lower()
    assert "library" in goal.observable_success_postcondition.lower()
    assert len(goal.fingerprint) == 24


def test_exact_direct_capability_succeeds_without_computer_use():
    goal = derive_semantic_goal("Search my account for invoice 42")
    direct = _mechanism(
        "direct:account_search",
        StrategyLayer.DIRECT_CAPABILITY,
        app="",
        surface="",
        account="user_account",
    )
    computer = _mechanism(
        "global_vision",
        StrategyLayer.GLOBAL_VISION,
        app="",
        surface="",
        account="user_account",
    )
    strategy = ExecutionStrategy(goal, (direct, computer))

    decision = _select(strategy, direct, {"query": "invoice 42"})
    assert decision.allowed is True
    strategy.record(
        decision,
        "succeeded",
        postcondition_evidence="direct_result:invoice_42_visible",
    )

    assert strategy.succeeded is True
    assert [attempt.mechanism_id for attempt in strategy.attempts] == [
        "direct:account_search"
    ]
    assert strategy.final_diagnostic()["code"] == "postcondition_verified"


def test_deep_link_succeeds_before_ui_navigation():
    goal = derive_semantic_goal("Open Steam at my Library")
    deep_link = _mechanism(
        "deep_link:steam:library", StrategyLayer.DEEP_LINK,
    )
    uia = _mechanism(
        "native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY,
    )
    strategy = ExecutionStrategy(goal, (deep_link, uia))

    # WOR-301: a lower mechanism is never refused because a higher one exists;
    # the model decides which available capability to use.
    lower_first = _select(strategy, uia)
    assert lower_first.allowed is True
    strategy.record(lower_first, "failed", failure_reason="controlled_failure")

    route = _select(strategy, deep_link, {"uri": "steam://open/games"})
    assert route.allowed is True
    strategy.record(
        route,
        "succeeded",
        postcondition_evidence="steam_library_route_verified",
    )

    assert strategy.succeeded is True
    assert all(
        attempt.status != "selected"
        for attempt in strategy.attempts
        if attempt.mechanism_id == "native_accessibility"
    )


def test_native_library_uia_partial_failure_then_keyboard_succeeds():
    goal = _steam_goal()
    uia = _mechanism("native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY)
    keyboard = _mechanism("keyboard_focus", StrategyLayer.KEYBOARD)
    strategy = ExecutionStrategy(goal, (uia, keyboard))

    first = _select(strategy, uia, {"target": "Library search field"})
    strategy.record(
        first,
        "failed",
        failure_reason="uia_partial_tree_no_actionable_rect",
        observation="Steam exposes only a partial accessibility subtree",
    )
    second = _select(strategy, keyboard, {"focus_target": "Library search field"})
    assert second.allowed is True
    strategy.record(
        second,
        "succeeded",
        postcondition_evidence="focused_library_results_show_subnautica",
    )

    assert strategy.succeeded is True
    assert [attempt.mechanism_id for attempt in strategy.attempts] == [
        "native_accessibility",
        "keyboard_focus",
    ]


def test_uia_and_keyboard_fail_then_local_zoomed_vision_succeeds():
    goal = _steam_goal()
    uia = _mechanism("native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY)
    keyboard = _mechanism("keyboard_focus", StrategyLayer.KEYBOARD)
    local = _mechanism("local_vision", StrategyLayer.LOCAL_VISION)
    strategy = ExecutionStrategy(goal, (uia, keyboard, local))

    for mechanism, reason in (
        (uia, "uia_target_missing"),
        (keyboard, "focus_cycle_without_match"),
    ):
        decision = _select(strategy, mechanism)
        assert decision.allowed is True
        strategy.record(decision, "stagnated", failure_reason=reason)

    zoomed = _select(strategy, local, {"crop": "library_search_region"})
    assert zoomed.allowed is True
    strategy.record(
        zoomed,
        "succeeded",
        postcondition_evidence="zoomed_crop_library_result_subnautica",
    )

    assert strategy.succeeded is True
    assert strategy.attempts[-1].mechanism == "local_or_zoomed_vision"


def test_zoomed_local_vision_does_not_depend_on_legacy_refinement_switch():
    from osia import agent

    assert not hasattr(agent, "_REFINE_DEFAULT")
    assert not hasattr(agent, "_vision_locate")
    assert not hasattr(agent, "_refine_click_target")


def test_global_vision_is_never_blocked_by_higher_applicable_levels():
    goal = _steam_goal()
    uia = _mechanism("native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY)
    keyboard = _mechanism("keyboard_focus", StrategyLayer.KEYBOARD)
    local = _mechanism("local_vision", StrategyLayer.LOCAL_VISION)
    global_vision = _mechanism("global_vision", StrategyLayer.GLOBAL_VISION)
    strategy = ExecutionStrategy(goal, (uia, keyboard, local, global_vision))

    # WOR-301: mechanism ranking never refuses an available capability.
    first = _select(strategy, global_vision)
    assert first.allowed is True
    strategy.record(first, "failed", failure_reason="controlled_failure")

    for mechanism in (uia, keyboard, local):
        decision = _select(strategy, mechanism)
        assert decision.allowed is True
        strategy.record(decision, "failed", failure_reason="controlled_failure")

    # The retry loop guard still requires new evidence before a replay
    # (execution-integrity), so observe one fresh revision first.
    strategy.observe(
        "fresh desktop state after failed attempts",
        evidence_token="state_revision_2",
    )
    last_resort = _select(strategy, global_vision)
    assert last_resort.allowed is True
    strategy.record(
        last_resort,
        "succeeded",
        postcondition_evidence="global_coordinates_then_library_postcondition",
    )
    assert strategy.succeeded is True


def test_dom_fallback_succeeds_when_surface_is_semantically_equivalent():
    goal = derive_semantic_goal(
        "Open the Steam Store and search for Subnautica"
    )
    dom = _mechanism(
        "dom:steam:store",
        StrategyLayer.DOM,
        surface="store",
        account="",
    )
    strategy = ExecutionStrategy(goal, (dom,))

    decision = _select(strategy, dom, {"url": "https://store.steampowered.com"})
    assert decision.allowed is True
    strategy.record(
        decision,
        "succeeded",
        postcondition_evidence="store_dom_results_show_subnautica",
    )
    assert strategy.succeeded is True


def test_dom_store_fallback_is_rejected_for_native_library_goal():
    goal = _steam_goal()
    store_dom = _mechanism(
        "dom:steam:store",
        StrategyLayer.DOM,
        surface="store",
        account="public",
    )
    strategy = ExecutionStrategy(goal, (store_dom,))

    decision = _select(
        strategy,
        store_dom,
        {"url": "https://store.steampowered.com/search/?term=Subnautica"},
    )

    assert decision.allowed is False
    assert decision.reason_code == "required_surface_changed"
    assert strategy.succeeded is False
    assert strategy.attempts[-1].status == "blocked"


def test_unknown_web_identity_is_not_refused_for_constrained_native_goal():
    request = "Open Steam, go to my Library and search for Subnautica"
    with execution_strategy_scope(request, {"navigate_browser"}) as strategy:
        decision = tool_preflight(
            "navigate_browser",
            {"url": "https://example.test/search?q=Subnautica"},
        )

    # WOR-301: navigating to a different web identity is the model's decision;
    # the strategy must not refuse an available browser capability for it.
    assert decision is None or decision.allowed is True


def test_live_adapter_allows_lower_levels_and_keeps_freshness_gate():
    request = "Open Steam, go to my Library and search for Subnautica"
    with execution_strategy_scope(
        request,
        {"open_uri", "desktop_click", "search_web", "navigate_browser"},
    ) as strategy:
        desktop_first = tool_preflight("desktop_click", {"description": "Library"})
        assert desktop_first is None or desktop_first.allowed is True

        cross_surface = tool_preflight(
            "navigate_browser",
            {"url": "https://store.steampowered.com/search/?term=Subnautica"},
        )
        assert cross_surface is None or cross_surface.allowed is True

        route = tool_preflight("open_uri", {"uri": "steam://open/games"})
        assert route is not None and route.allowed is True
        record_tool_outcome(
            "open_uri",
            route,
            outcome="success_unverified",
            is_error=False,
            reason_code="",
            result="FIRED: steam://open/games",
        )
        assert strategy.succeeded is False

        desktop_after_route = tool_preflight(
            "desktop_click", {"description": "Library search field"},
        )
        assert desktop_after_route is not None
        assert desktop_after_route.allowed is False
        assert desktop_after_route.reason_code == "exact_desktop_target_observation_required"


def test_stale_target_must_be_reresolved_before_retry():
    goal = derive_semantic_goal("Open the Steam Store and select Subnautica")
    dom = _mechanism(
        "dom:steam:store", StrategyLayer.DOM, surface="store", account="",
    )
    strategy = ExecutionStrategy(goal, (dom,))
    action = {"element_ref": "element_stale"}

    first = _select(strategy, dom, action)
    strategy.record(first, "stale", failure_reason="stale_semantic_reference")

    unchanged = _select(strategy, dom, action)
    assert unchanged.allowed is False
    assert unchanged.reason_code == "stale_target_requires_reresolution"

    lower = _mechanism("keyboard_focus", StrategyLayer.KEYBOARD)
    fallback_without_observation = _select(strategy, lower)
    assert fallback_without_observation.allowed is False
    assert fallback_without_observation.reason_code == (
        "stale_target_requires_reresolution"
    )

    strategy.observe(
        "fresh DOM snapshot resolved a replacement target",
        evidence_token="state_revision_2",
    )
    refreshed = _select(strategy, dom, {"element_ref": "element_fresh"})
    assert refreshed.allowed is True


def test_window_presence_does_not_complete_a_surface_specific_goal():
    with execution_strategy_scope(
        'Search the Steam Library for "Hades"', {"has_window"},
    ) as strategy:
        record_tool_observation("has_window", "window: yes", is_error=False)

    assert strategy.goal.required_surface == "library"
    assert strategy.succeeded is False


def test_same_failed_action_cannot_loop_without_new_evidence():
    goal = derive_semantic_goal("Open the Steam Store and select Subnautica")
    dom = _mechanism(
        "dom:steam:store", StrategyLayer.DOM, surface="store", account="",
    )
    strategy = ExecutionStrategy(goal, (dom,))
    action = {"element_ref": "element_1"}

    first = _select(strategy, dom, action)
    strategy.record(first, "failed", failure_reason="element_not_actionable")
    replay = _select(strategy, dom, action)
    assert replay.allowed is False
    assert replay.reason_code == "identical_failed_action_without_new_evidence"

    strategy.observe("element became enabled", evidence_token="enabled_revision_2")
    evidence_driven_retry = _select(strategy, dom, action)
    assert evidence_driven_retry.allowed is True

    strategy.record(
        evidence_driven_retry, "failed", failure_reason="element_not_actionable",
    )
    strategy.observe("same snapshot", evidence_token="enabled_revision_2")
    duplicate_evidence_retry = _select(strategy, dom, action)
    assert duplicate_evidence_retry.allowed is False
    # WOR-303: the refusal is factual (same failed action, no new evidence),
    # never a count of attempts.
    assert duplicate_evidence_retry.reason_code == (
        "identical_failed_action_without_new_evidence"
    )


def test_ambiguous_side_effect_freezes_all_alternatives_until_reconciliation():
    goal = derive_semantic_goal("Send the controlled test message")
    direct = _mechanism(
        "direct:send", StrategyLayer.DIRECT_CAPABILITY,
        app="", surface="", account="", mutation=True,
    )
    dom = _mechanism(
        "dom:test:composer", StrategyLayer.DOM,
        app="", surface="", account="", mutation=True,
    )
    strategy = ExecutionStrategy(goal, (direct, dom))

    mutation = _select(strategy, direct, {"message_ref": "disposable_1"})
    strategy.record(
        mutation,
        "ambiguous",
        failure_reason="accepted_but_terminal_receipt_missing",
    )

    fallback = _select(strategy, dom, {"message_ref": "disposable_1"})
    assert fallback.allowed is False
    assert fallback.reason_code == "ambiguous_effect_requires_reconciliation"
    assert strategy.final_diagnostic()["next_action"] == "reconcile_existing_attempt"

    strategy.reconcile(succeeded=True, evidence="delivery_receipt_disposable_1")
    assert strategy.succeeded is True


def test_negative_deterministic_observation_reconciles_ambiguous_effect():
    with execution_strategy_scope(
        "Open Calculator", {"desktop_click", "has_window"},
    ) as strategy:
        direct = StrategyMechanism(
            "direct:open_application:calculator",
            StrategyLayer.DIRECT_CAPABILITY,
            "controlled launch",
            covers_goal=True,
            mutation=True,
        )
        decision = strategy.select(direct, {"name": "calculator"})
        strategy.record(
            decision, "ambiguous", failure_reason="launch_receipt_missing",
        )
        assert strategy.frozen_reason == "launch_receipt_missing"

        record_tool_observation("has_window", "window: no", is_error=False)

    assert strategy.frozen_reason == ""
    assert strategy.succeeded is False


def test_all_valid_strategies_exhausted_returns_precise_failure_contract():
    goal = _steam_goal()
    uia = _mechanism("native_accessibility", StrategyLayer.NATIVE_ACCESSIBILITY)
    keyboard = _mechanism("keyboard_focus", StrategyLayer.KEYBOARD)
    strategy = ExecutionStrategy(goal, (uia, keyboard))

    for mechanism, reason in (
        (uia, "uia_tree_did_not_expose_target"),
        (keyboard, "focus_cycle_completed_without_target"),
    ):
        decision = _select(strategy, mechanism)
        strategy.record(decision, "stagnated", failure_reason=reason)

    diagnostic = strategy.final_diagnostic()
    assert diagnostic == {
        "code": "all_semantically_valid_strategies_exhausted",
        "goal_fingerprint": goal.fingerprint,
        "remaining": [],
        "uia_candidate_exhaustion": {
            "tier_state": "unknown",
            "pending_substep": {},
            "candidate_count": 0,
            "candidate_pattern_pair_count": 0,
            "retired_candidate_count": 0,
            "remaining_candidate_count": 0,
            "remaining_candidates": [],
        },
        "failure_reasons": [
            "uia_tree_did_not_expose_target",
            "focus_cycle_completed_without_target",
        ],
        "postcondition_evidence": [],
        "next_action": "precise_failure_or_clarification",
    }


def test_controlled_end_to_end_smoke_changes_strategy_and_preserves_goal():
    """First controlled mechanism fails; the next one satisfies the exact goal."""

    goal = _steam_goal()
    direct = _mechanism(
        "direct:synthetic_library_search",
        StrategyLayer.DIRECT_CAPABILITY,
    )
    deep_link = _mechanism(
        "deep_link:steam:library",
        StrategyLayer.DEEP_LINK,
        covers=False,
    )
    keyboard = _mechanism("keyboard_focus", StrategyLayer.KEYBOARD)
    strategy = ExecutionStrategy(goal, (direct, deep_link, keyboard))
    fixture_results = {
        direct.mechanism_id: ("failed", "synthetic_direct_unavailable", ""),
        deep_link.mechanism_id: (
            "progress", "", "steam_library_route_observed",
        ),
        keyboard.mechanism_id: (
            "succeeded", "", "library_results_show_subnautica",
        ),
    }

    for mechanism in (direct, deep_link, keyboard):
        decision = _select(strategy, mechanism)
        assert decision.allowed is True
        outcome, reason, evidence = fixture_results[mechanism.mechanism_id]
        strategy.record(
            decision,
            outcome,
            failure_reason=reason,
            postcondition_evidence=evidence,
        )
        if strategy.succeeded:
            break

    assert strategy.succeeded is True
    assert [attempt.mechanism_id for attempt in strategy.attempts] == [
        "direct:synthetic_library_search",
        "deep_link:steam:library",
        "keyboard_focus",
    ]
    assert all(
        attempt.mechanism_id != "dom:steam:store"
        for attempt in strategy.attempts
    )
    assert strategy.goal.required_surface == "library"
    assert strategy.postcondition_evidence[-1] == "library_results_show_subnautica"
