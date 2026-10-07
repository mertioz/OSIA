"""Regression coverage for universal shortest-reliable-path prompt guidance."""

from osia.agent import SYSTEM_PROMPT_CORE, get_system_prompt
from osia.computer_use_v4_integration import computer_use_v4_turn_scope


def test_canonical_guidance_prefers_simple_reliable_paths_without_fixed_routing():
    prompt = SYSTEM_PROMPT_CORE
    guidance = next(
        line for line in prompt.splitlines()
        if "equally suitable and reliable" in line
    )

    assert "prefer the simplest, most direct path" in guidance
    assert "fewest unnecessary interactions" in guidance
    assert "contextual preference, not a fixed tool order" in guidance
    assert "Direct, deterministic operations" in guidance
    assert "needless GUI steps" in guidance
    assert (
        "Computer Use remains appropriate when visual observation or interaction is needed"
        in guidance
    )
    assert "Never trade correctness, required observation, factual verification, or safety" in guidance
    assert "especially for consequential actions" in guidance
    assert len(guidance.split()) <= 75
    assert "Steam" not in guidance


def test_synthetic_tool_choice_scenarios_remain_contextual_and_model_facing():
    # Opening an installed app: direct launch and GUI capabilities remain visible.
    with computer_use_v4_turn_scope(
        "deepseek", "deepseek-flash",
        conversation_id="conversation-shortest-path-synthetic",
        turn_id="turn-shortest-path-synthetic",
    ):
        prompt = get_system_prompt("en", "deepseek-flash", "deepseek")
    assert "When multiple available approaches are equally suitable and reliable" in prompt
    assert "- open_application(" in prompt
    assert "- computer_actions(" in prompt

    # Interacting inside an app still has Computer Use available when needed.
    assert "Computer Use remains appropriate when visual observation or interaction is needed" in prompt

    # A consequential submit/delete cannot bypass factual verification or safety.
    assert "Never trade correctness, required observation, factual verification, or safety" in prompt
    assert "especially for consequential actions" in prompt


def test_existing_screenshot_and_redundant_verification_guidance_is_preserved():
    prompt = SYSTEM_PROMPT_CORE

    assert "An unchanged screenshot proves neither failure nor success." in prompt
    assert (
        "A distinct fresh model-requested action remains available after success, failure, or OUTCOME_UNKNOWN."
        in prompt
    )
    assert "never repeat a successful mutation" not in prompt
