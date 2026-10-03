"""The selected model sees factual outcomes without a second planning authority."""

import inspect

from osia.agent import OSIAAgent
from osia.execution_strategy import build_execution_strategy
from osia.goal_contract import goal_contract_from_semantic_mission
from osia.reasoning import build_reasoning_request


def test_fact_prompt_does_not_consult_applicability_frontier(monkeypatch):
    strategy = build_execution_strategy(
        "Open Steam and go to my Library.",
        {"open_uri", "write_file", "run_command", "desktop_click"},
    )
    strategy.bind_goal_contract(goal_contract_from_semantic_mission(strategy.mission))

    def forbidden_frontier():
        raise AssertionError("model context must not consult a tool selector")

    monkeypatch.setattr(strategy, "remaining_mechanisms", forbidden_frontier)
    for prompt in (strategy.model_instruction(), strategy.model_feedback()):
        assert strategy.mission.objective in prompt
        assert "unverified_outcomes" in prompt
        assert "full available capability surface" in prompt
        assert "applicable_capabilities" not in prompt
        assert "all_semantically_valid_strategies_exhausted" not in prompt
    assert strategy.required_postconditions[0] in strategy.premature_final_instruction()


def test_obsolete_adaptive_environment_cannot_override_selected_reasoning(monkeypatch):
    monkeypatch.setenv("OSIA_ADAPTIVE_REASONING_AUTO", "1")
    monkeypatch.setenv("OSIA_ADAPTIVE_INTELLIGENCE_PHASE3", "1")
    agent = object.__new__(OSIAAgent)
    agent.provider = "openai"
    agent.model = "gpt-5.5"
    agent._reasoning_override_for_turn = "high"
    assert agent._reasoning_request() == build_reasoning_request(
        agent.provider, agent.model,
        settings={"default": "high", "providers": {}, "models": {}},
    )
    driver = inspect.getsource(OSIAAgent._drive_terminal_turn)
    assert "adaptive_runtime" not in driver
    assert "adaptive_success_eligible" not in driver
    assert "execution_guidance" not in inspect.getsource(OSIAAgent._run_turn)
