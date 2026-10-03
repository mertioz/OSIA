from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


_V3 = {"request_visual_observation", "desktop_click"}


def test_v3_is_dormant_but_legacy_dispatchers_remain_importable():
    from osia import agent as agent_module
    from osia.tool_contracts import get_tool_spec, static_tool_inventory_specs

    assert not (_V3 & set(agent_module._active_chat_tool_names()))
    assert not (_V3 & {spec.name for spec in static_tool_inventory_specs()})
    assert all(name in agent_module.TOOL_MAP for name in _V3)
    assert callable(agent_module.tool_request_visual_observation)
    assert callable(agent_module.tool_desktop_click)
    for name in _V3:
        spec = get_tool_spec(name)
        assert spec is not None
        # WOR-301: dormancy is technical; no mode-eligibility field exists.
        assert not hasattr(spec, "chat_eligible")
        assert not hasattr(spec, "autonomous_eligible")
        assert not hasattr(spec, "scheduled_eligible")


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only V4 native frontier",
)
def test_account_native_graph_has_v4_and_no_v3_for_vision_model():
    from osia.agent import _active_chat_tool_names
    from osia.chatgpt_account.app_server_client import _canonical_native_tools
    from osia.computer_use_v4_integration import (
        computer_use_v4_test_scope,
        computer_use_v4_turn_scope,
    )
    from osia.execution_strategy import build_execution_strategy

    strategy = build_execution_strategy(
        "Clique le contrôle visuel dans la fenêtre contrôlée.",
        _active_chat_tool_names(),
    )
    with computer_use_v4_test_scope(vision_capable=True), (
        computer_use_v4_turn_scope(
            "chatgpt_account",
            "gpt-5.6-terra",
            conversation_id="cutover_vision",
            turn_id="cutover_vision_turn",
        )
    ):
        names = {item["name"] for item in _canonical_native_tools(strategy)}
    assert "computer_screenshot" in names
    assert "computer_click" in names
    assert not (_V3 & names)


def test_nonvision_model_has_no_visual_fallback_but_keeps_structured_path():
    from osia.agent import _active_chat_tool_names
    from osia.chatgpt_account.app_server_client import _canonical_native_tools
    from osia.computer_use_v4_integration import (
        computer_use_v4_test_scope,
        computer_use_v4_turn_scope,
    )
    from osia.execution_strategy import build_execution_strategy

    strategy = build_execution_strategy(
        "Inspecte la fenêtre native contrôlée.",
        _active_chat_tool_names(),
    )
    with computer_use_v4_test_scope(vision_capable=False), (
        computer_use_v4_turn_scope(
            "chatgpt_account",
            "gpt-5.3-codex-spark",
            conversation_id="cutover_nonvision",
            turn_id="cutover_nonvision_turn",
        )
    ):
        names = {item["name"] for item in _canonical_native_tools(strategy)}
    assert not any(name.startswith("computer_") for name in names)
    assert not (_V3 & names)
    assert "inspect_desktop_surfaces" in names


def test_residual_v3_call_is_typed_and_same_model_continues(monkeypatch):
    from osia import agent as agent_module

    dispatched = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "desktop_click",
        lambda **_kwargs: dispatched.append(True) or "unexpected dispatch",
    )

    class Completions:
        def __init__(self):
            self.calls = []
            self.replies = [
                '<tool_call>{"tool":"desktop_click",'
                '"description":"controlled target"}</tool_call>',
                "The controlled request was not executed.",
            ]

        def create(self, **kwargs):
            self.calls.append(kwargs)
            content = (
                self.replies.pop(0)
                if self.replies else "The controlled request was not executed."
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=content,
                tool_calls=None,
            ))])

    completions = Completions()
    osia = agent_module.OSIAAgent(lang="en")
    osia.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = osia.run("Use the controlled visual desktop path.")

    assert "unfinished" in result
    assert dispatched == []
    assert len(completions.calls) >= 2
    assert any(
        "legacy Vision/Mouse V3 is dormant" in repr(
            call.get("messages") or []
        )
        for call in completions.calls[1:]
    )
