"""Mouse-disabled chats keep keyboard/UIA actions without pointer decoration."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from osia import conversations, desktop_activity as activity
from osia.slash_commands import chat_command_scope


@pytest.fixture
def chat_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    directory = tmp_path / "feedback"
    monkeypatch.setattr(activity, "activity_directory", lambda: directory)
    disabled = conversations.create_conversation(special_mode=True)["id"]
    enabled = conversations.create_conversation()["id"]
    assert activity._depth == 0
    return disabled, enabled, directory


def test_disabled_mouse_keeps_native_keyboard_without_feedback(chat_profile, monkeypatch):
    from osia.tools import desktop_control as desktop

    disabled, enabled, directory = chat_profile
    events = []
    backend = SimpleNamespace(
        write=lambda text: events.append(("type", text, activity.desktop_activity_visible())),
        press=lambda key: events.append(("key", key, activity.desktop_activity_visible())),
    )
    monkeypatch.setattr(desktop, "_backend", lambda: "pyautogui")
    monkeypatch.setattr(desktop, "_pyautogui_ready_error", lambda: None)
    monkeypatch.setattr(desktop, "_intended_foreground_error", lambda: None)
    monkeypatch.setattr(desktop, "_load_pyautogui", lambda: backend)
    with chat_command_scope(disabled, None):
        assert desktop.type_text("test") == "typed 4 characters"
        assert desktop.press_key("Tab") == "pressed Tab"
    assert events == [("type", "test", False), ("key", "tab", False)]
    assert not list(directory.glob("*.json"))

    with chat_command_scope(enabled, None):
        assert desktop.type_text("test") == "typed 4 characters"
    assert events[-1] == ("type", "test", True)


def test_attempt_intent_reads_canonical_mouse_setting_before_chat_scope(chat_profile, tmp_path):
    from osia.tool_attempts import ToolAttemptStore

    disabled, enabled, _directory = chat_profile
    store = ToolAttemptStore(tmp_path / "attempts")
    for conversation_id, turn_id in ((disabled, "disabled-turn"), (enabled, "enabled-turn")):
        attempt = store.create_attempt(
            conversation_id=conversation_id, request_id="request", turn_id=turn_id,
            tool_call_id="call", tool_name="desktop_type", provider_id="fixture", model_id="fixture",
            turn_context={"language": "en", "source": "fixture"},
            argument_fingerprint=store.fingerprint_arguments({"text": "test"}),
            side_effect="external_mutation", idempotence="non_idempotent", retry_policy="never_after_dispatch",
        )
        assert attempt["status"] == "not_started"
        assert activity.desktop_activity_visible() is (conversation_id == enabled)
    activity.finish_desktop_intent("enabled-turn")


@pytest.mark.parametrize("mouse_disabled", [True, False])
def test_uia_worker_keeps_the_originating_feedback_preference(chat_profile, mouse_disabled):
    from osia import desktop_scene

    disabled, enabled, _directory = chat_profile
    seen = []
    control = SimpleNamespace(
        AutomationId="observed-control", GetRuntimeId=lambda: (1, 2),
        GetChildren=lambda: [],
        GetInvokePattern=lambda: SimpleNamespace(Invoke=lambda: seen.append(activity.desktop_activity_visible())),
    )
    auto = SimpleNamespace(
        UIAutomationInitializerInThread=nullcontext,
        ControlFromHandle=lambda _hwnd: control,
    )
    with chat_command_scope(disabled if mouse_disabled else enabled, None):
        result = desktop_scene._dispatch_fresh_uia_action_bounded(
            auto, native_window_id=42, runtime_id=(1, 2),
            action_pattern="Invoke", observed_patterns=("Invoke",),
        )
    assert result["completed"] and result["dispatch_started"]
    assert seen == [not mouse_disabled]


def test_disabled_chat_does_not_clear_feedback_from_a_concurrent_enabled_chat(chat_profile):
    disabled, enabled, _directory = chat_profile
    with chat_command_scope(enabled, None), activity.native_input_activity():
        assert activity._depth == 1
        with chat_command_scope(disabled, None), activity.native_input_activity():
            assert activity._depth == 1
            assert activity.desktop_activity_visible()
        assert activity._depth == 1
    assert activity._depth == 0


def test_unreadable_preference_only_suppresses_decoration(chat_profile, monkeypatch):
    def unavailable():
        raise OSError("profile unavailable")

    monkeypatch.setattr("osia.slash_commands.current_chat_commands", unavailable)
    with activity.native_input_activity():
        assert not activity.desktop_activity_visible()
    assert activity._depth == 0
