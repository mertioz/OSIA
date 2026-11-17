"""Conversation control blocks new captures/pointer effects, not existing input."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest

from osia import conversations, desktop_activity as activity
from osia.slash_commands import chat_command_scope, current_control_disabled


@pytest.fixture
def control_chats(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(activity, "activity_directory", lambda: tmp_path / "feedback")
    off = conversations.create_conversation(control_disabled=True)["id"]
    on = conversations.create_conversation()["id"]
    assert activity._depth == 0
    return off, on


def test_control_toggle_persists_and_has_no_legacy_coupling(control_chats):
    off, on = control_chats
    conv = conversations.get_conversation(off)
    conv["special_mode"] = True
    conv["messages"] = [{"role": "user", "content": "Retain this history"}]
    conversations._path_for(off).write_text(json.dumps(conv), encoding="utf-8")
    assert conversations.conversation_control_disabled(off)
    assert not conversations.conversation_control_disabled(on)
    restored = conversations.set_conversation_control_disabled(off)
    assert restored["control_disabled"] is False
    assert restored["special_mode"] is True
    assert restored["messages"] == conv["messages"]
    disabled = conversations.set_conversation_control_disabled(off)
    assert disabled["control_disabled"] is True
    assert disabled["revision"] == restored["revision"] + 1
    assert {c["id"]: c["control_disabled"] for c in conversations.list_conversations()} == {off: True, on: False}
    with chat_command_scope(off, None):
        assert current_control_disabled()
        with chat_command_scope(on, None):
            assert not current_control_disabled()
        assert current_control_disabled()
    assert not current_control_disabled()


def test_control_toggle_keeps_existing_busy_and_first_request_integrity(control_chats):
    _off, on = control_chats
    assert conversations.claim_background_execution(on, "active-turn")
    try:
        with pytest.raises(ValueError, match="conversation_busy"):
            conversations.set_conversation_control_disabled(on)
        assert not conversations.conversation_control_disabled(on)
    finally:
        assert conversations.release_background_execution(on, "active-turn")
    first, created = conversations.resolve_request_conversation("control-unbound", control_disabled=True)
    replay, created_again = conversations.resolve_request_conversation("control-unbound", control_disabled=True)
    assert created and not created_again and replay["id"] == first["id"]
    assert first["control_disabled"] is True


def test_control_contract_blocks_only_new_capture_and_pointer_calls(control_chats):
    from osia import agent
    from osia.tool_contracts import get_tool_spec, tool_disabled_by_chat_command

    off, on = control_chats
    blocked = {"take_screenshot", "save_screenshot", "computer_screenshot", "computer_click", "computer_double_click", "computer_scroll", "desktop_scroll"}
    with chat_command_scope(off, None):
        prompt = agent.get_system_prompt("en")
        assert "Control disabled for this conversation" in prompt
        assert "Keyboard input, structured browser tools" in prompt
        assert "analysis of images already provided" in prompt
        assert all(tool_disabled_by_chat_command(name) for name in blocked)
        assert not any(tool_disabled_by_chat_command(name) for name in ("computer_type", "computer_keypress", "computer_zoom", "computer_observation", "desktop_type", "desktop_press", "get_browser_state", "fill_form_field", "click", "press_key"))
        assert tool_disabled_by_chat_command("computer_actions", {"actions": [{"kind": "keypress"}, {"kind": "click"}]})
        assert not tool_disabled_by_chat_command("computer_actions", {"actions": [{"kind": "keypress"}]})
        schema = get_tool_spec("computer_actions").input_schema
        assert schema["properties"]["actions"]["items"]["properties"]["kind"]["enum"] == ["type", "keypress"]
    with chat_command_scope(on, None):
        assert not any(tool_disabled_by_chat_command(name) for name in blocked)
        assert "click" in get_tool_spec("computer_actions").input_schema["properties"]["actions"]["items"]["properties"]["kind"]["enum"]


def test_control_guards_native_primitives_before_backend_or_focus(control_chats, monkeypatch, tmp_path):
    from osia import agent, desktop_scene
    from osia.tools import desktop_control as desktop

    off, _on = control_chats
    monkeypatch.setattr(desktop, "_backend", lambda: pytest.fail("pointer backend loaded"))
    monkeypatch.setattr(desktop_scene, "_observe_exact_bound_visual_scene", lambda *_a, **_k: pytest.fail("target inspection before disabled capture"))
    with chat_command_scope(off, None):
        for result in (desktop.prepare_click(1, 2), desktop.dispatch_prepared_click(None), desktop.move_to(1, 2), desktop.scroll(), agent.tool_desktop_scroll()):
            assert "CONVERSATION_CONTROL_DISABLED" in result
        assert not desktop._screenshot_with_cursor(str(tmp_path / "screen.png"), {})
        assert "CONVERSATION_CONTROL_DISABLED" in agent._capture_screen(str(tmp_path / "screen.png"))
        for invoke in (
            lambda: desktop_scene._capture_bounds_timed(None),
            lambda: desktop_scene._capture_exact_window_image(0, None),
            lambda: desktop_scene._capture_bound_visual_surface(None, None, provider="fixture", model="fixture"),
        ):
            with pytest.raises(RuntimeError, match="CONVERSATION_CONTROL_DISABLED"):
                invoke()
        visual = desktop_scene.request_visual_observation("surface", "target")
        assert "CONVERSATION_CONTROL_DISABLED" in json.dumps(visual)
    assert not (tmp_path / "screen.png").exists()


def test_control_guards_v4_before_loop_window_inspection_or_activation(control_chats, monkeypatch, tmp_path):
    from osia import computer_use_v4_integration as v4
    from osia.computer_use_v4 import ComputerUseV4, ComputerUseV4Error, ComputerAction
    from osia.tool_attempts import ToolAttemptStore

    off, _on = control_chats
    monkeypatch.setattr(v4, "_loop", lambda: pytest.fail("V4 loop entered before control guard"))
    loop = ComputerUseV4(
        provider="fixture", model="fixture", turn_id="capture-turn",
        window_state=lambda _hwnd: pytest.fail("window inspected"),
        capture_window=lambda *_args: pytest.fail("capture called"),
        foreground_activation=lambda *_args: pytest.fail("window activated"),
        action_store=ToolAttemptStore(tmp_path / "attempts"),
    )
    with chat_command_scope(off, None), v4.computer_use_v4_test_scope(vision_capable=True), v4.computer_use_v4_turn_scope("fixture", "fixture", conversation_id=off, turn_id="control-turn"):
        for result in (v4.tool_computer_screenshot(), v4.tool_save_screenshot(), v4.tool_computer_click("frame", 1, 2), v4.tool_computer_scroll("frame"), v4.tool_computer_actions([{"kind": "click", "frame_id": "frame", "x": 1, "y": 2}])):
            assert result["receipt"]["reason_code"] == "CONVERSATION_CONTROL_DISABLED"
        with pytest.raises(ComputerUseV4Error, match="CONVERSATION_CONTROL_DISABLED") as captured:
            loop.screenshot(42)
        assert captured.value.detail == "new_screenshots_disabled"
        result = loop.execute(ComputerAction("click", "frame", x=1, y=2))
        assert result["status"] == "CONVERSATION_CONTROL_DISABLED" and result["dispatch_count"] == 0
        batch = loop.execute_batch([
            ComputerAction("keypress", "frame", key="Enter"),
            ComputerAction("click", "frame", x=1, y=2),
        ])
        assert batch["status"] == "CONVERSATION_CONTROL_DISABLED" and batch["executed_count"] == 0
    loop.close()


def test_control_context_reaches_browser_owner_and_keeps_structured_result(control_chats):
    from osia.tools import browser

    off, on = control_chats
    calls = []
    owner = browser._AsyncBrowserOwner()

    async def screenshot(**_kwargs):
        calls.append("captured")
        return b"an existing fixture image"

    page = SimpleNamespace(screenshot=screenshot)

    async def probe():
        return {"text": "structured result", "screenshot": await owner._screenshot_b64(page)}

    owner._op_test_capture = probe
    try:
        with chat_command_scope(off, None):
            assert owner.call("test_capture") == {"text": "structured result", "screenshot": None}
        assert calls == []
        with chat_command_scope(on, None):
            assert owner.call("test_capture")["screenshot"]
        assert calls == ["captured"]
    finally:
        owner.close()


def test_control_disabled_does_not_decorate_keyboard_or_uia_or_another_chat(control_chats):
    from osia import desktop_scene

    off, on = control_chats
    with chat_command_scope(on, None), activity.native_input_activity():
        assert activity._depth == 1
        with chat_command_scope(off, None), activity.native_input_activity():
            assert activity._depth == 1
        assert activity.desktop_activity_visible()
    assert activity._depth == 0
    seen = []
    control = SimpleNamespace(AutomationId="observed", GetRuntimeId=lambda: (1, 2), GetChildren=lambda: [], GetInvokePattern=lambda: SimpleNamespace(Invoke=lambda: seen.append(activity._depth)))
    auto = SimpleNamespace(UIAutomationInitializerInThread=nullcontext, ControlFromHandle=lambda _hwnd: control)
    with chat_command_scope(off, None):
        result = desktop_scene._dispatch_fresh_uia_action_bounded(auto, native_window_id=42, runtime_id=(1, 2), action_pattern="Invoke", observed_patterns=("Invoke",))
    assert result["completed"] and result["dispatch_started"] and seen == [0]
    activity.observe_desktop_intent("off-turn", "desktop_type", conversation_id=off)
    # The earlier enabled activity may still be inside its brief visual hold.
    assert not activity.desktop_activity_visible(now=__import__('time').time() + 5)
