"""WOR-151 response-style prompt regressions."""

from __future__ import annotations

import inspect

from osia import agent, personality
from osia.models import PROVIDERS


EN_STYLE = (
    "Reason carefully and thoroughly internally when the task requires it. "
    "Keep explanations to the user concise and direct without sacrificing "
    "accuracy or necessary completeness. Do not reveal private chain-of-thought; "
    "provide conclusions, relevant evidence, and concise reasoning summaries instead."
)
FR_STYLE = (
    "Raisonne avec soin et en profondeur en interne lorsque la tâche l’exige. "
    "Garde les explications destinées à l’utilisateur concises et directes sans "
    "sacrifier l’exactitude ni les éléments nécessaires. Ne révèle pas la chaîne "
    "de pensée privée ; fournis plutôt les conclusions, les preuves pertinentes "
    "et un résumé concis du raisonnement."
)


def _stable_personality(monkeypatch):
    monkeypatch.setattr(personality, "load_personality", lambda: personality.DEFAULT_PERSONALITY)
    monkeypatch.setattr(personality, "load_enabled_skills_text", lambda: "")


def test_new_instruction_appears_exactly_once_and_old_generic_copy_is_removed(
    monkeypatch,
):
    _stable_personality(monkeypatch)
    english = agent.get_system_prompt("en", "model", "provider")
    french = agent.get_system_prompt("fr", "model", "provider")
    assert english.count(EN_STYLE) == 1
    assert french.count(FR_STYLE) == 1
    assert "helpful, concise, and friendly" not in english
    assert "Be concise." not in english
    assert "concise" not in personality.DEFAULT_PERSONALITY.lower()


def test_instruction_preserves_depth_accuracy_completeness_and_privacy(monkeypatch):
    _stable_personality(monkeypatch)
    prompt = agent.get_system_prompt("en", "model", "provider")
    assert "carefully and thoroughly internally" in prompt
    assert "concise and direct" in prompt
    assert "without sacrificing accuracy or necessary completeness" in prompt
    assert "Do not reveal private chain-of-thought" in prompt
    assert "conclusions, relevant evidence" in prompt
    assert "reveal your chain-of-thought" not in prompt.lower()


def test_all_supported_providers_receive_the_same_compatible_instruction(monkeypatch):
    _stable_personality(monkeypatch)
    for provider in (*PROVIDERS, "chatgpt_account"):
        prompt = agent.get_system_prompt("en", "test-model", provider)
        assert prompt.count(EN_STYLE) == 1


def test_tool_security_and_anti_loop_contracts_are_unchanged(monkeypatch):
    _stable_personality(monkeypatch)
    prompt = agent.get_system_prompt("en", "model", "provider")
    assert "## How to Use Tools" in prompt
    assert "## Honesty about tool results (CRITICAL)" in prompt
    assert 'starts with "ERROR:"' in prompt
    guard_source = inspect.getsource(agent._AntiLoopGuard)
    assert "class _AntiLoopGuard" in guard_source
    assert "record" in guard_source
