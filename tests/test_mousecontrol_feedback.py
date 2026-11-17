"""The retired conversation flag never restricts native input or its feedback."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from osia import conversations, desktop_activity as activity
from osia.slash_commands import chat_command_scope, SlashTurnDirectivesV1


@pytest.fixture
def legacy_chat(tmp_path, monkeypatch):
    directory = tmp_path / "conversations"
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", directory)
    feedback = tmp_path / "feedback"
    monkeypatch.setattr(activity, "activity_directory", lambda: feedback)
    conv = conversations.create_conversation()
    conv["special_mode"] = True
    conv["unrelated_legacy_metadata"] = {"keep": True}
    (directory / (conv["id"] + ".json")).write_text(json.dumps(conv), encoding="utf-8")
    assert activity._depth == 0
    return conv["id"], feedback


def test_legacy_flag_is_preserved_as_data_without_a_mouse_setting_api(legacy_chat):
    conv_id, _feedback = legacy_chat
    assert conversations.get_conversation(conv_id)["special_mode"] is True
    assert not hasattr(conversations, "conversation_special_mode")
    assert not hasattr(conversations, "set_conversation_special_mode")
    assert "special_mode" not in next(item for item in conversations.list_conversations() if item["id"] == conv_id)
    assert conversations.rename_conversation(conv_id, "Renamed")
    stored = conversations.get_conversation(conv_id)
    assert stored["special_mode"] is True
    assert stored["unrelated_legacy_metadata"] == {"keep": True}


def test_legacy_flag_does_not_change_tools_batches_or_prompt(legacy_chat):
    from osia import agent
    from osia.tool_contracts import get_tool_spec, tool_disabled_by_chat_command

    conv_id, _feedback = legacy_chat
    actions = [{"kind": "click", "frame_id": "frame", "x": 10, "y": 20}]
    with chat_command_scope(conv_id, None):
        for name in ("desktop_scroll", "computer_click", "computer_double_click", "computer_scroll"):
            assert not tool_disabled_by_chat_command(name)
        assert not tool_disabled_by_chat_command("computer_actions", {"actions": actions})
        kinds = get_tool_spec("computer_actions").input_schema["properties"]["actions"]["items"]["properties"]["kind"]["enum"]
        assert {"click", "double_click", "scroll", "type", "keypress"} == set(kinds)
        assert "Conversation setting: mouse control disabled" not in agent.get_system_prompt("en")
        assert tool_disabled_by_chat_command("create_chat_skill")
    with chat_command_scope(conv_id, SlashTurnDirectivesV1(skill_creator=True)):
        assert not tool_disabled_by_chat_command("create_chat_skill")


def test_legacy_flag_does_not_suppress_native_keyboard_feedback(legacy_chat, monkeypatch):
    from osia.tools import desktop_control as desktop

    conv_id, _feedback = legacy_chat
    seen = []
    backend = SimpleNamespace(write=lambda text: seen.append((text, activity.desktop_activity_visible())))
    monkeypatch.setattr(desktop, "_backend", lambda: "pyautogui")
    monkeypatch.setattr(desktop, "_pyautogui_ready_error", lambda: None)
    monkeypatch.setattr(desktop, "_intended_foreground_error", lambda: None)
    monkeypatch.setattr(desktop, "_load_pyautogui", lambda: backend)
    with chat_command_scope(conv_id, None):
        assert desktop.type_text("test") == "typed 4 characters"
    assert seen == [("test", True)]


def test_native_pointer_dispatch_does_not_consult_legacy_flag(legacy_chat, monkeypatch):
    from osia import agent, computer_use_v4_integration as v4
    from osia.tools import desktop_control as desktop

    conv_id, _feedback = legacy_chat
    calls = []
    monkeypatch.setattr(desktop, "scroll", lambda direction, amount: calls.append((direction, amount)) or "scrolled")

    def execute(action):
        calls.append(action.kind)
        return {"status": "DISPATCHED_VERIFIED", "dispatch_count": 1, "action_kind": action.kind, "frame_id": action.frame_id}

    def execute_batch(actions):
        calls.extend(action.kind for action in actions)
        return {"status": "DISPATCHED_VERIFIED", "executed_count": len(actions), "results": []}

    monkeypatch.setattr(v4, "_loop", lambda: SimpleNamespace(execute=execute, execute_batch=execute_batch))
    with chat_command_scope(conv_id, None), v4.computer_use_v4_turn_scope(
        "chatgpt_account", "fixture", conversation_id=conv_id, turn_id="legacy-native",
    ):
        assert "disabled" not in str(agent.tool_desktop_scroll("down", 2)).lower()
        assert v4.tool_computer_click("frame", 10, 20)["receipt"]["callback_occurred"]
        assert v4.tool_computer_scroll("frame")["receipt"]["callback_occurred"]
        batch = v4.tool_computer_actions([
            {"kind": "click", "frame_id": "frame", "x": 10, "y": 20},
            {"kind": "keypress", "frame_id": "frame", "key": "Enter"},
        ])
        assert batch["receipt"]["batch_executed_count"] == 2
    assert calls == [("down", 2), "click", "scroll", "click", "keypress"]


def test_legacy_flag_does_not_suppress_intent(legacy_chat):
    conv_id, _feedback = legacy_chat
    with chat_command_scope(conv_id, None):
        activity.observe_desktop_intent("legacy-turn", "desktop_type")
    assert activity.desktop_activity_visible()
    activity.finish_desktop_intent("legacy-turn")
    assert not activity.desktop_activity_visible()


def test_uia_action_and_feedback_are_independent_of_legacy_flag(legacy_chat):
    from osia import desktop_scene

    conv_id, _feedback = legacy_chat
    seen = []
    control = SimpleNamespace(
        AutomationId="observed-control", GetRuntimeId=lambda: (1, 2), GetChildren=lambda: [],
        GetInvokePattern=lambda: SimpleNamespace(Invoke=lambda: seen.append(activity.desktop_activity_visible())),
    )
    auto = SimpleNamespace(UIAutomationInitializerInThread=nullcontext, ControlFromHandle=lambda _hwnd: control)
    with chat_command_scope(conv_id, None):
        result = desktop_scene._dispatch_fresh_uia_action_bounded(
            auto, native_window_id=42, runtime_id=(1, 2), action_pattern="Invoke", observed_patterns=("Invoke",),
        )
    assert result["completed"] and result["dispatch_started"]
    assert seen == [True]
