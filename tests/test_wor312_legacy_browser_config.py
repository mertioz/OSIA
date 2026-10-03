from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

from osia import config
from osia.tools.browser_controller import BrowserController


class _RegistryKey:
    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeWinReg:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1

    def __init__(self, values: dict[str, str]):
        self.values = values
        self.deleted: list[str] = []

    def OpenKey(self, _root, path: str):
        if path not in self.values:
            raise OSError("not registered")
        return _RegistryKey(path)

    def QueryValueEx(self, key: _RegistryKey, _name: str):
        return self.values[key.path], self.REG_SZ

    def DeleteKey(self, _root, path: str):
        self.deleted.append(path)
        self.values.pop(path, None)


def test_legacy_personal_browser_settings_fall_back_to_isolated_runtime(
    tmp_path, monkeypatch,
):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({
            "personal_browser_enabled": True,
            "personal_browser_preference": "brave",
            "personal_browser_instance_hint": "old-target-handle",
            "ui_theme": "azure",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_path))
    monkeypatch.setattr(config, "_settings_cache_signature", None)
    monkeypatch.setattr(config, "_settings_cache_data", {})

    settings = config.load_settings()

    assert settings["ui_theme"] == "azure"
    assert "personal_browser_enabled" not in settings
    assert "personal_browser_preference" not in settings
    assert "personal_browser_instance_hint" not in settings
    assert BrowserController().selected_backend_name() == "isolated"


def test_removed_browser_backend_override_is_rejected_as_unknown_metadata():
    from osia.slash_commands import SlashDirectiveError, SlashTurnDirectivesV1

    try:
        SlashTurnDirectivesV1.from_wire({
            "schema_version": 1,
            "connector_hints": [],
            "capability_requirements": {"current_web": False},
            "conversation_refs": [],
            "browser_backend_override": "personal",
        })
    except SlashDirectiveError as error:
        assert error.code == "unknown_directive_metadata"
    else:
        raise AssertionError("obsolete browser mode was accepted")


def test_retired_session_files_are_absent_and_isolated_browser_remains():
    assert not Path("osia/personal_browser").exists()
    assert not Path("osia/tools/browser_backends/personal.py").exists()
    assert not Path("extension/personal-browser").exists()
    assert not Path("packaging/personal_browser").exists()
    assert not Path("packaging/native_messaging/space.osia.browser.json.in").exists()

    browser_runtime = Path("osia/tools/browser.py").read_text(encoding="utf-8")
    assert "def open_browser(" in browser_runtime
    assert "def _detect_chromium_browser(" in browser_runtime
    controller = Path("osia/tools/browser_controller.py").read_text(
        encoding="utf-8",
    )
    assert 'return "isolated"' in controller
    assert "PersonalBrowserBackend" not in controller
    launcher = Path("packaging/windows/launcher.py").read_text(
        encoding="utf-8",
    )
    assert "remove_legacy_browser_registration" in launcher
    assert "repair_owned_registration" not in launcher

    mobile_settings = Path(
        "../osia-mobile/lib/screens/providers_screen.dart"
    ).read_text(encoding="utf-8")
    assert "_BrowserPreferenceScreen" not in mobile_settings
    assert "personal_browser_preference" not in mobile_settings


def test_legacy_cleanup_removes_only_manifest_paths_marked_as_osia_owned(
    tmp_path, monkeypatch,
):
    from osia import legacy_browser_cleanup as cleanup

    state_root = tmp_path / "legacy"
    state_root.mkdir()
    manifest = state_root / "native_host.json"
    marker_path = state_root / "registration.json"
    manifest.write_text("{}", encoding="utf-8")
    marker_path.write_text(json.dumps({
        "owner": "space.osia.browser",
        "manifest_path": str(manifest),
    }), encoding="utf-8")
    chrome = r"Software\Google\Chrome\NativeMessagingHosts\space.osia.browser"
    edge = r"Software\Microsoft\Edge\NativeMessagingHosts\space.osia.browser"
    other_manifest = str(tmp_path / "other.json")
    registry = _FakeWinReg({chrome: str(manifest), edge: other_manifest})
    monkeypatch.setattr(cleanup, "_STATE_ROOT", state_root)
    monkeypatch.setattr(cleanup, "_MANIFEST", manifest)
    monkeypatch.setattr(cleanup, "_MARKER", marker_path)
    monkeypatch.setattr(cleanup.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", registry)

    result = cleanup.remove_legacy_browser_registration()

    assert result == {"state": "not_registered", "removed": True}
    assert registry.deleted == [chrome]
    assert registry.values == {edge: other_manifest}
    assert not manifest.exists()
    assert not marker_path.exists()


def test_legacy_cleanup_leaves_unowned_registration_and_files_untouched(
    tmp_path, monkeypatch,
):
    from osia import legacy_browser_cleanup as cleanup

    state_root = tmp_path / "legacy"
    state_root.mkdir()
    manifest = state_root / "native_host.json"
    marker_path = state_root / "registration.json"
    manifest.write_text("{}", encoding="utf-8")
    marker_path.write_text(json.dumps({
        "owner": "another.application",
        "manifest_path": str(manifest),
    }), encoding="utf-8")
    monkeypatch.setattr(cleanup, "_STATE_ROOT", state_root)
    monkeypatch.setattr(cleanup, "_MANIFEST", manifest)
    monkeypatch.setattr(cleanup, "_MARKER", marker_path)
    monkeypatch.setattr(cleanup.sys, "platform", "win32")

    result = cleanup.remove_legacy_browser_registration()

    assert result == {"state": "not_registered", "removed": False}
    assert manifest.exists()
    assert marker_path.exists()
