"""Application discovery and truthful failures without changing the live desktop."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osia import agent, desktop_scene
from osia.desktop_surface import DesktopSurfaceRegistry
from osia.tools import desktop_control as desktop
from osia.tool_contracts import ExecutionOutcome
from test_wor258_desktop_surfaces import _registered_visual_scene


def _background_scene(monkeypatch, *, second=False):
    registry = DesktopSurfaceRegistry()
    first = replace(_registered_visual_scene(registry), foreground=False)
    scenes = {7100: first}
    windows = [(7100, "Messenger", 7200, "Fixture")]
    if second:
        scenes[7101] = replace(_registered_visual_scene(registry, hwnd=7101, pid=7201), foreground=False)
        windows.append((7101, "Messenger", 7201, "Fixture"))
    monkeypatch.setattr(desktop_scene, "DESKTOP_SURFACE_REGISTRY", registry)
    monkeypatch.setattr(desktop_scene.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_load_win32", lambda: (object(), object()))
    monkeypatch.setattr(desktop_scene, "observe_foreground_scene", lambda **kwargs: None)
    monkeypatch.setattr(desktop, "_enum_app_windows_win32", lambda: windows)
    monkeypatch.setattr(desktop_scene, "_process_details", lambda pid: ("messenger", 10.0))
    monkeypatch.setattr(desktop_scene, "_observe_window_scene", lambda window, **kwargs: scenes[window[0]])
    monkeypatch.setattr(desktop_scene, "_current_launch_target", lambda *args: None)
    return first


def test_explicit_background_app_is_inspectable_without_a_readable_foreground(monkeypatch):
    first = _background_scene(monkeypatch)
    result = desktop_scene.inspect_desktop_surfaces("Messenger")
    assert result["scene"]["window_surface_ref"] == first.window_ref.ref_id
    assert result["receipt"]["target_foreground"] is False
    assert result["scene"]["foreground_application"] == ""


def test_unreadable_foreground_still_preserves_target_ambiguity(monkeypatch):
    _background_scene(monkeypatch, second=True)
    result = desktop_scene._select_desktop_target("Messenger")
    assert result.status == "target_ambiguous" and result.target_scene is None


def test_no_implicit_target_is_invented_when_foreground_is_unavailable(monkeypatch):
    _background_scene(monkeypatch)
    result = desktop_scene._select_desktop_target("")
    assert result.status == "desktop_surface_unavailable" and result.target_scene is None


def test_unavailable_native_api_does_not_claim_an_application_is_missing(monkeypatch):
    _background_scene(monkeypatch)
    monkeypatch.setattr(desktop, "_load_win32", lambda: None)
    result = desktop_scene._select_desktop_target("Messenger")
    assert result.status == "desktop_surface_unavailable" and result.target_scene is None


@pytest.mark.parametrize("value,expected", [
    (r"C:\Program Files (x86)\Steam\Steam.exe", "steam"),
    (r'C:\Apps With Spaces\Example.exe --option', "example"),
    (r'"C:\Apps With Spaces\Example.exe" --option', "example"),
    ("notepad.exe note.txt", "notepad"),
])
def test_full_executable_path_keeps_its_real_application_identity(value, expected):
    assert desktop._norm_app_token(value) == expected


def test_missing_absolute_executable_is_rejected_before_any_dispatch(monkeypatch):
    monkeypatch.setattr(agent.sys, "platform", "win32")
    monkeypatch.setattr(desktop.os.path, "isfile", lambda path: False)
    dispatch = Mock(side_effect=AssertionError("known missing executable must not dispatch"))
    monkeypatch.setitem(agent.TOOL_MAP, "open_application", dispatch)
    result = agent._execute_tool_with_policy("open_application", {"name": r"C:\Apps\Messenger\app-old\Messenger.exe"})
    assert result.outcome == ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert result.dispatched is False and result.worker_may_be_active is False
    assert result.reason_code == "application_executable_missing"
    dispatch.assert_not_called()


def test_existing_absolute_executable_keeps_the_original_argument_and_dispatch(monkeypatch):
    monkeypatch.setattr(agent.sys, "platform", "win32")
    monkeypatch.setattr(desktop.os.path, "isfile", lambda path: True)
    launch = Mock(return_value="launch fixture returned")
    monkeypatch.setitem(agent.TOOL_MAP, "open_application", launch)
    name = r"C:\Apps With Spaces\Example.exe"
    result = agent._execute_tool_with_policy("open_application", {"name": name})
    launch.assert_called_once_with(name=name)
    assert result.result == "launch fixture returned" and result.dispatched is True


def test_existing_integrity_rejection_keeps_priority_over_missing_path(monkeypatch):
    monkeypatch.setattr(agent.sys, "platform", "win32")
    monkeypatch.setattr(desktop.os.path, "isfile", lambda path: False)
    launch = Mock(side_effect=AssertionError("rejected call must never dispatch"))
    monkeypatch.setitem(agent.TOOL_MAP, "open_application", launch)
    result = agent._execute_tool_with_policy("open_application", {"name": r"C:\Missing\Example.exe"},
                                            preflight_error="ERROR: BLOCK_SYSTEM_DESTRUCTION",
                                            preflight_reason_code="block_system_destruction")
    assert result.outcome == ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert result.reason_code == "block_system_destruction"
    assert result.dispatched is False
    launch.assert_not_called()


def test_application_name_lookup_and_non_windows_launches_keep_their_contract(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop.application_launch_error("Messenger") is None
    assert desktop.application_launch_error("notepad.exe note.txt") is None
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert desktop.application_launch_error(r"C:\Missing\Example.exe") is None


def test_direct_legacy_launch_also_never_spawns_a_known_missing_file(monkeypatch):
    monkeypatch.setattr(agent.sys, "platform", "win32")
    monkeypatch.setattr(desktop.os.path, "isfile", lambda path: False)
    focus = Mock(return_value=("unavailable", ""))
    spawn = Mock(side_effect=OSError("not started"))
    monkeypatch.setattr(desktop_scene, "focus_existing_application_window", focus)
    monkeypatch.setattr(desktop, "find_app_window", lambda name: None)
    monkeypatch.setattr(desktop, "resolve_windows_app", lambda name: None)
    monkeypatch.setattr(desktop, "find_app_window_multi", lambda names: None)
    monkeypatch.setattr(desktop, "foreground_snapshot", lambda: None)
    monkeypatch.setattr(agent, "_lookup_memory_app_route", lambda name: None)
    monkeypatch.setattr(agent.subprocess, "Popen", spawn)
    result = agent.tool_open_application(r"C:\Missing\Example.exe")
    assert "No process was started" in result
    focus.assert_not_called()
    spawn.assert_not_called()


def test_renderer_window_can_belong_to_exact_application_process_tree(monkeypatch):
    import psutil
    parent = SimpleNamespace(name=lambda: "MainApp.exe", exe=lambda: r"C:\Apps\MainApp\MainApp.exe", parent=lambda: None)
    renderer = SimpleNamespace(name=lambda: "renderer.exe", exe=lambda: r"C:\Apps\MainApp\bin\renderer.exe", parent=lambda: parent)
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_load_win32", lambda: (object(), object()))
    monkeypatch.setattr(desktop, "_enum_app_windows_win32", lambda: [(33, "Main window", 44, "Fixture")])
    monkeypatch.setattr(psutil, "Process", lambda pid: renderer)
    assert desktop.find_app_window_multi({"mainapp"}) == (33, "Main window", 44)


def test_shared_shell_ancestor_never_proves_application_identity(monkeypatch):
    import psutil
    shell = SimpleNamespace(name=lambda: "explorer.exe", exe=lambda: r"C:\Windows\explorer.exe")
    renderer = SimpleNamespace(name=lambda: "foreign.exe", exe=lambda: r"C:\Windows\foreign.exe", parent=lambda: shell)
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_load_win32", lambda: (object(), object()))
    monkeypatch.setattr(desktop, "_enum_app_windows_win32", lambda: [(33, "Foreign", 44, "Fixture")])
    monkeypatch.setattr(psutil, "Process", lambda pid: renderer)
    assert desktop.find_app_window_multi({"explorer"}) is None


def test_process_parent_in_another_install_root_does_not_prove_window_identity(monkeypatch):
    import psutil
    parent = SimpleNamespace(name=lambda: "MainApp.exe", exe=lambda: r"C:\Apps\MainApp\MainApp.exe", parent=lambda: None)
    renderer = SimpleNamespace(name=lambda: "other.exe", exe=lambda: r"D:\Other\other.exe", parent=lambda: parent)
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_load_win32", lambda: (object(), object()))
    monkeypatch.setattr(desktop, "_enum_app_windows_win32", lambda: [(33, "Other", 44, "Fixture")])
    monkeypatch.setattr(psutil, "Process", lambda pid: renderer)
    assert desktop.find_app_window_multi({"mainapp"}) is None
