"""Persistent desktop choices and the two normal custom-connector entry points."""
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from urllib.parse import urlunsplit
import xml.etree.ElementTree as ET

import pytest

from osia import config, windows_store
from osia.mcp_client import custom
from osia.server import run_server


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(tmp_path / "credentials.json"))
    return tmp_path


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("granted", [False, True])
def test_permission_prompt_once_per_capability_across_processes(profile, app, monkeypatch, granted):
    from osia.gui import network_permissions as ui
    monkeypatch.setattr(windows_store, "is_store_install", lambda: True)
    monkeypatch.setattr(windows_store, "equivalent_mobile_firewall_choice", lambda: None)
    replies = []
    def answer(*args):
        replies.append(1)
        return ui.QMessageBox.StandardButton.Yes if granted else ui.QMessageBox.StandardButton.No
    monkeypatch.setattr(ui.QMessageBox, "question", answer)
    for _ in range(2):
        assert ui.request_mobile_permission() is granted
    assert replies == [1]
    assert windows_store.permission_decision("another-capability") is None
    windows_store.remember_permission("another-capability", not granted)
    code = ("import sys; from osia import config,windows_store as w; config.SETTINGS_FILE=sys.argv[1]; "
            "assert w.permission_decision(w.MOBILE_NETWORK_PERMISSION) is " + str(granted) +
            "; assert w.permission_decision('another-capability') is " + str(not granted))
    subprocess.run([sys.executable, "-c", code, config.SETTINGS_FILE], check=True)
    assert ui.request_mobile_permission(review=True) is granted
    assert replies == [1, 1]


@pytest.mark.parametrize("decision", [None, False, True])
def test_store_external_listeners_require_a_recorded_grant(profile, monkeypatch, decision):
    monkeypatch.setattr(windows_store, "is_store_install", lambda: True)
    monkeypatch.setattr(run_server, "_lan_coordination_enabled", lambda: True)
    if decision is not None:
        windows_store.remember_permission(windows_store.MOBILE_NETWORK_PERMISSION, decision)
    calls = []
    assert run_server._bind_host() == "127.0.0.1"
    assert run_server._launch_after_port_guard(port_guard=lambda: True, start_lan=lambda: calls.append("lan"),
                                              start_relay=lambda: calls.append("signaling"),
                                              run_server=lambda: calls.append("loopback"))
    assert calls == (["lan", "signaling", "loopback"] if decision else ["loopback"])


def test_packaged_firewall_rules_expose_only_mobile_ports():
    root = ET.parse(Path(__file__).resolve().parents[1] / "packaging/windows/store/AppxManifest.xml")
    rules = root.find("./{*}Extensions/{*}Extension/{*}FirewallRules")
    assert rules.attrib["Executable"] == r"app\OSIA.exe"
    assert {(r.get("IPProtocol"), r.get("LocalPortMin"), r.get("LocalPortMax"),
             r.get("Direction")) for r in rules} == {("TCP", "8766", "8766", "in"), ("UDP", "5353", "5353", "in")}


def test_migration_preserves_equivalent_native_decisions_and_never_widens_a_partial_grant():
    apps = PureWindowsPath("C:/Program Files/WindowsApps")
    exe = apps / "GreatInvention.OSIA_1.9.279.0_x64__ge7crsndb7e8c/app/OSIA.exe"
    def rules(action, scopes):
        profiles = "|".join(f"Profile={p}" for p in scopes)
        return [(label + " Query User{fixture}" + str(exe),
                 f"Active=TRUE|Dir=In|Action={action}|App={exe}|Protocol={protocol}|{profiles}|")
                for label, protocol in (("TCP", "6"), ("UDP", "17"))]
    assert windows_store.previous_firewall_choice(rules("Allow", ["Public"]), apps) is None
    full = rules("Allow", ["Public", "Private", "Domain"])
    assert windows_store.previous_firewall_choice(full, apps) is True
    assert windows_store.previous_firewall_choice(rules("Block", ["All"]), apps) is False
    assert windows_store.previous_firewall_choice(full + rules("Block", ["Public"]), apps) is None
    assert windows_store.previous_firewall_choice(full, apps.parent) is None


def test_portable_mobile_defaults_on_and_remembers_disabling(profile, monkeypatch):
    from osia import portable_mobile as mobile
    from osia.utils import systemd_manager as services
    class Process:
        def __init__(self, cmd):
            self.closed = False
        def poll(self):
            return 0 if self.closed else None
        def terminate(self):
            self.closed = True
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(mobile.subprocess, "Popen", Process)
    monkeypatch.setattr(mobile, "_command", None)
    monkeypatch.setattr(mobile, "_child", None)
    mobile.initialize(["fixture"])
    unit = "osia-server.service"
    assert services.is_service_enabled(unit) and services.get_service_status(unit) == "running"
    assert services.disable_service(unit)
    mobile.initialize(["updated-fixture"])
    assert not services.is_service_enabled(unit) and services.get_service_status(unit) == "stopped"
    assert services.enable_service(unit) and services.get_service_status(unit) == "running"


def test_chat_adds_a_disconnected_connector_in_the_same_settings(profile):
    from osia.agent import TOOL_MAP
    from osia.tool_contracts import get_tool_spec, validate_tool_arguments
    name = "add_mcp_server"
    data = {"name": "GitHub", "transport": "https", "url": "https://example.org/mcp", "auth_type": "token"}
    validate_tool_arguments(get_tool_spec(name), data)
    result = TOOL_MAP[name](**data)
    assert result["receipt"]["outcome"] == "confirmed_success"
    saved, = config.get_setting("mcp_servers")
    assert saved["id"] == "github-2" and saved["transport"] == "streamable_http"
    assert saved["enabled"] is False and not saved["env"]
    bad = TOOL_MAP[name](name="Malformed", transport="https", url="http://example.org/mcp")
    assert bad["receipt"]["outcome"] == "confirmed_failure_before_effect"
    assert config.get_setting("mcp_servers") == [saved]


def test_concurrent_custom_names_preserve_connectors(profile):
    cfg = custom.server_config("My Server", "stdio", command="node", args=["server.js"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: custom.save_custom_server(cfg), range(4)))
    assert len(set(ids)) == 4
    assert {s["id"] for s in config.get_setting("mcp_servers")} == set(ids)


@pytest.mark.parametrize("url", ["http://example.org/mcp", "https://",
                                 urlunsplit(("https", "fixture:fixture@example.org", "/", "", "")),
                                 "https://example.org/mcp?api_key=example", "https://example.org:bad/"])
def test_https_field_rejects_insecure_or_secret_urls(url):
    with pytest.raises(custom.InvalidServer):
        custom.server_config("Document search", "https", url=url)


def test_settings_tabs_preserve_input_and_submit_only_active_transport(app, profile):
    from osia.gui.main_popup import _McpCustomDialog
    from osia.mcp_client.reconnect import build_headless_connect_cfg

    dlg = _McpCustomDialog(None)
    try:
        dlg.show()
        dlg._name.setText("Documents")
        dlg._url.setText("https://example.org/mcp")
        assert dlg._url.isVisible() and not dlg._command.isVisible()
        dlg._tabs.setCurrentIndex(1)
        assert dlg._command.isVisible() and not dlg._url.isVisible()
        dlg._command.setText('node "a file.js"')
        dlg._on_ok()
        assert dlg.result_cfg["args"] == ["a file.js"] and not dlg.result_cfg["url"]
        dlg._tabs.setCurrentIndex(0)
        dlg._auth.setCurrentIndex(dlg._auth.findData("token"))
        dlg._token.setText("test-value")
        dlg._on_ok()
        assert dlg.result_cfg["command"] is None and not dlg.result_cfg["args"]
        sid = custom.save_custom_server(dlg.result_cfg, dlg.result_token)
        saved, = config.get_setting("mcp_servers")
        assert config.get_mcp_token(sid) == "test-value"
        assert "test-value" not in Path(config.SETTINGS_FILE).read_text("utf-8")
        assert build_headless_connect_cfg(saved)["headers"] == {"Authorization": "Bearer test-value"}
    finally:
        dlg.close()


def test_registered_stdio_server_can_really_connect_and_call(profile, tmp_path):
    from osia.mcp_client.manager import MCPManager
    path = tmp_path / "local fixture.py"
    path.write_text("""from mcp.server.fastmcp import FastMCP
m = FastMCP("fixture")

@m.tool()
def ping() -> str:
    return "pong"

m.run()
""", encoding="utf-8")
    cfg = custom.server_config("Local fixture", "stdio", command=sys.executable, args=[str(path)])
    sid = custom.save_custom_server(cfg)
    mgr = MCPManager()
    try:
        assert mgr.connect(config.get_setting("mcp_servers")[0]) == "connected"
        assert "pong" in str(mgr.call_tool(sid, "ping", {}))
    finally:
        mgr.shutdown()


def test_registered_https_server_can_really_connect_and_call(profile, tmp_path, monkeypatch):
    import datetime
    import ipaddress
    import socket
    import threading
    import time

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from mcp.server.fastmcp import FastMCP
    from osia.mcp_client.manager import MCPManager
    import uvicorn

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    certfile = tmp_path / "fixture.crt"
    keyfile = tmp_path / "fixture.key"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    monkeypatch.setenv("SSL_CERT_FILE", str(certfile))
    fixture = FastMCP("HTTPS fixture")

    @fixture.tool()
    def ping() -> str:
        return "pong"

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(fixture.streamable_http_app(), ssl_certfile=str(certfile),
                                                ssl_keyfile=str(keyfile), log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        manager = MCPManager()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            cfg = custom.server_config("Remote fixture", "https", url=f"https://127.0.0.1:{port}/mcp")
            sid = custom.save_custom_server(cfg)
            assert manager.connect(config.get_setting("mcp_servers")[0]) == "connected"
            assert "pong" in str(manager.call_tool(sid, "ping", {}))
        finally:
            manager.shutdown()
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()
