"""Per-profile launch-at-login defaults on each desktop distribution."""
import json
import os
from pathlib import Path
import plistlib
import sys
import uuid

import pytest

from osia import config
from osia.utils import assets


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", str(root))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(root / "settings.json"))
    monkeypatch.setattr(assets.windows_store, "is_store_install", lambda: False)
    monkeypatch.setattr(assets, "portable_desktop_executable", lambda: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return root


def test_server_first_and_later_linux_opt_out(profile, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Linux")
    launcher = profile / 'OSIA 100% $edition' / "OSIA"
    monkeypatch.setattr(assets, "portable_desktop_executable", lambda: launcher)
    config.ensure_config_files(create_token=False)
    entry = assets._autostart_desktop_path()
    assert not entry.exists()  # A headless bootstrap owns no login side effect.
    assert assets.initialize_autostart()
    assert '100%% ' in entry.read_text(encoding="utf-8")
    assert str(launcher.name) in entry.read_text(encoding="utf-8")
    assert assets.autostart_enabled()
    entry.write_text(entry.read_text(encoding="utf-8") + "Hidden=true\n", encoding="utf-8")
    assert assets.initialize_autostart() and not assets.autostart_enabled()
    assert assets.set_autostart(True) and assets.autostart_enabled()
    assert assets.set_autostart(False)
    config.ensure_config_files(create_token=False)
    assert assets.initialize_autostart() and not entry.exists()


def test_legacy_profile_and_existing_hidden_entry_are_preserved(profile, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Linux")
    Path(config.SETTINGS_FILE).write_text("{}", encoding="utf-8")
    config.ensure_config_files(create_token=False)
    assert assets.initialize_autostart() and not assets._autostart_desktop_path().exists()
    entry = assets._autostart_desktop_path()
    entry.parent.mkdir(parents=True)
    original = "[Desktop Entry]\nType=Application\nExec=custom\nHidden=true\n"
    entry.write_text(original, encoding="utf-8")
    config.desktop_autostart_pending_file().touch()
    assert assets.initialize_autostart()
    assert entry.read_text(encoding="utf-8") == original


def test_managed_linux_uses_stable_command_across_updates(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "0.9.279"
    launcher = release / "bin" / "osia"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    stable = tmp_path / ".local" / "bin" / "osia"
    stable.parent.mkdir(parents=True)
    os.link(launcher, stable)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(assets, "portable_desktop_executable", lambda: None)
    monkeypatch.setattr(assets, "PROJECT_ROOT", str(release / "app"))
    monkeypatch.setattr(assets, "application_executable", lambda: release / "python/bin/python3.12")
    assert assets._unix_startup_arguments() == [str(stable)]


@pytest.mark.parametrize("edition", ["portable", "installer", "store"])
def test_windows_default_respects_installation_owner(edition, profile, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Windows")
    monkeypatch.setattr(assets, "is_frozen", lambda: True)
    monkeypatch.setattr(assets, "application_executable", lambda: profile / "OSIA.exe")
    monkeypatch.setattr(assets.windows_store, "is_store_install", lambda: edition == "store")
    monkeypatch.setattr(assets, "_windows_run_registered", lambda: False)
    actions = []
    monkeypatch.setattr(assets, "_set_autostart_windows", lambda enabled: actions.append(enabled) or True)
    monkeypatch.setattr(assets.windows_store, "startup_task", lambda *args: pytest.fail("startup request during initialization"))
    if edition == "installer":
        (profile / "unins000.exe").touch()
        (profile / "unins000.dat").touch()
    config.ensure_config_files(create_token=False)
    assert assets.initialize_autostart() and assets.initialize_autostart()
    assert actions == ([True] if edition == "portable" else [])


@pytest.mark.skipif(sys.platform != "win32", reason="requires the Windows registry")
def test_native_windows_run_value_and_opt_out_use_the_same_command(profile, monkeypatch):
    import winreg

    key = "Software\\OSIA-Login-Test-" + uuid.uuid4().hex
    executable = profile / "OSIA portable" / "OSIA.exe"
    monkeypatch.setattr(assets, "_WIN_RUN_KEY", key)
    monkeypatch.setattr(assets, "is_frozen", lambda: True)
    monkeypatch.setattr(assets, "application_executable", lambda: executable)
    try:
        config.ensure_config_files(create_token=False)
        assert assets.initialize_autostart() and assets.autostart_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            assert winreg.QueryValueEx(handle, "OSIA") == (f'"{executable}" --wake', winreg.REG_SZ)
        assert assets.set_autostart(False) and assets.initialize_autostart()
        assert not assets.autostart_enabled()
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


def test_retry_after_failure_but_never_after_explicit_disable(profile, monkeypatch):
    actions = []
    def configure(enabled, *, initial=False):
        actions.append((enabled, initial))
        return not initial
    monkeypatch.setattr(assets, "_configure_autostart", configure)
    config.set_setting("time_format", "24h")  # Fresh profile written before GUI startup.
    assert not assets.initialize_autostart()
    assert config.desktop_autostart_pending_file().is_file()
    assert assets.set_autostart(False) and assets.initialize_autostart()
    assert actions == [(True, True), (False, False)]
    assert json.loads(Path(config.SETTINGS_FILE).read_bytes())["time_format"] == "24h"


def test_bundled_mac_uses_permanent_launcher_and_honors_external_opt_out(profile, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    plist = profile / "Library/LaunchAgents/space.osia.desktop.login.plist"
    monkeypatch.setattr(assets, "_mac_launch_agent", lambda: plist)
    launcher = profile / "OSIA.app/Contents/MacOS/OSIA"
    monkeypatch.setattr(assets, "portable_desktop_executable", lambda: profile / "AppTranslocation" / "OSIA")
    config.ensure_config_files(create_token=False)
    assert not assets.initialize_autostart()
    assert not plist.exists() and config.desktop_autostart_pending_file().exists()
    monkeypatch.setattr(assets, "portable_desktop_executable", lambda: launcher)
    assert assets.initialize_autostart() and assets.autostart_enabled()
    entry = plistlib.loads(plist.read_bytes())
    assert entry["ProgramArguments"] == [str(launcher)]
    assert entry["AssociatedBundleIdentifiers"] == ["space.osia.desktop"]
    entry["Disabled"] = True
    plist.write_bytes(plistlib.dumps(entry))
    assert assets.initialize_autostart() and not assets.autostart_enabled()
    assert assets.set_autostart(False) and not plist.exists()


def test_unbundled_mac_login_plist_uses_arguments_and_respects_removal(profile, monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    plist = profile / "Library/LaunchAgents/space.osia.desktop.login.plist"
    monkeypatch.setattr(assets, "_mac_launch_agent", lambda: plist)
    monkeypatch.setattr(assets, "PROJECT_ROOT", str(profile / "OSIA Source"))
    config.ensure_config_files(create_token=False)
    assert assets.initialize_autostart() and assets.autostart_enabled()
    entry = plistlib.loads(plist.read_bytes())
    assert entry["ProgramArguments"][-2:] == [str(profile / "OSIA Source" / "osia/main.py"), "--wake"]
    assert entry["RunAtLoad"] and "KeepAlive" not in entry
    plist.unlink()  # Removed outside OSIA.
    assert assets.initialize_autostart() and not assets.autostart_enabled()
