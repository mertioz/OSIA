from pathlib import Path
import xml.etree.ElementTree as ET

from osia import updater, windows_store
from osia.utils import assets, systemd_manager as services


def test_native_store_identity_does_not_apply_to_source():
    assert not windows_store.is_store_install()


def test_store_updates_and_login_belong_to_the_package(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_store, "is_store_install", lambda: True)
    monkeypatch.setattr(updater, "is_store_install", lambda: True)
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    monkeypatch.setattr(updater.sys, "platform", "win32")
    assert not updater.updater_enabled()
    assert windows_store.supervisor_shutdown_event("server") != r"Local\OSIA.SupervisorShutdown.server"
    called = []
    monkeypatch.setattr(windows_store, "startup_task", lambda task, enabled=None: called.append((task, enabled)) or True)
    monkeypatch.setattr(assets.platform, "system", lambda: "Windows")
    assert assets.autostart_enabled() and assets.set_autostart(False)
    assert called == [(windows_store.GUI_STARTUP, None), (windows_store.GUI_STARTUP, False)]
    monkeypatch.setattr(services, "_IS_WINDOWS", True)
    monkeypatch.setattr(services, "is_frozen", lambda: True)
    monkeypatch.setattr(services, "_SERVER_DISABLED_MARKER", tmp_path / "disabled")
    monkeypatch.setattr(services, "_win_task_registered", lambda *_: (_ for _ in ()).throw(AssertionError("login task during launch")))
    monkeypatch.setattr(services, "_win_should_start", lambda kind: True)
    monkeypatch.setattr(windows_store, "start_server", lambda: called.append("start") or True)
    assert services.ensure_service_running("osia-server.service") and called[-1] == "start"
    assert not services._win_is_supervisor({"cmdline": ["foreign.exe", "--osia-service", "server"]}, "server")
    assert not services._win_end_task("OSIA Server")
    (tmp_path / "disabled").touch()
    before = list(called)
    assert services.ensure_service_running("osia-server.service") and called == before


def test_store_manifest_matches_the_public_product():
    path = Path(__file__).resolve().parents[1] / "packaging/windows/store/AppxManifest.xml"
    manifest = ET.parse(path)
    ns = {"p": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
          "d": "http://schemas.microsoft.com/appx/manifest/desktop/windows10"}
    assert manifest.find("p:Identity", ns).get("Name") == windows_store.PACKAGE_FAMILY.split("_")[0]
    tasks = manifest.findall(".//d:StartupTask", ns)
    assert {t.get("TaskId"): t.get("Enabled") for t in tasks} == {
        windows_store.GUI_STARTUP: "true",
        windows_store.SERVER_STARTUP: "false",
    }
