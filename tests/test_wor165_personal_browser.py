from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import time
import sys

import pytest

from osia.personal_browser import protocol
from osia.personal_browser.broker import (
    PersonalBrowserBroker,
    _Client,
    _PendingRequest,
    _effect_error,
    _wrong_personal_reference,
)
from osia.personal_browser.registration import (
    DEVELOPMENT_EXTENSION_ID,
    allowed_origins,
)
from osia.tools.browser_backends.isolated import IsolatedBrowserBackend
from osia.tools.browser_controller import (
    BrowserController,
    browser_turn_scope,
    current_browser_turn_binding,
)


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "personal-browser"


class RecordingBackend:
    def __init__(self, name: str, result: dict | None = None):
        self.name = name
        self.result = result or {"ok": True, "backend": name}
        self.calls = []

    def invoke(self, operation, arguments):
        self.calls.append((operation, dict(arguments)))
        return dict(self.result)

    def status(self):
        return {"backend": self.name, "state": "ready"}

    def capabilities(self):
        return {"backend": self.name, "operations": []}

    def close(self):
        self.calls.append(("close", {}))

    def revoke(self):
        return {"ok": True}


def _target(handle="target:aaaaaaaa", session="personal_aaaaaaaa"):
    return {
        "target_handle": handle,
        "install_instance_id": "install_aaaaaaaa",
        "browser_session_id": session,
        "browser_family": "chrome",
        "window_id": 1,
        "tab_id": 2,
        "safe_origin": "https://example.test",
        "safe_url": "https://example.test/page",
        "safe_title": "Example",
        "authorization_epoch": 1,
        "document_nonce": "document:aaaaaaaa",
        "state_ref": {"browser_session_id": session},
        "authorized": True,
        "state": "ready",
    }


def _client(connection="connection:aaaaaaaa", session="personal_aaaaaaaa"):
    return _Client(
        connection_id=connection,
        install_instance_id="install_aaaaaaaa",
        browser_session_id=session,
        browser_family="chrome",
        caller_origin=f"chrome-extension://{DEVELOPMENT_EXTENSION_ID}/",
        capabilities=("get_browser_state",),
        handle=object(),
    )


def _broker_without_listener(client=None):
    broker = object.__new__(PersonalBrowserBroker)
    broker._lock = threading.RLock()
    broker._clients = {client.connection_id: client} if client else {}
    broker._pending = {}
    broker._progress_callback = None
    broker._closed = False
    broker._server_thread = None
    return broker


def test_controller_defaults_to_isolated_and_personal_is_exactly_turn_scoped():
    isolated = RecordingBackend("isolated")
    personal = RecordingBackend("personal")
    controller = BrowserController(isolated=isolated, personal=personal)
    assert controller.invoke("get_browser_state", {})["backend"] == "isolated"
    with browser_turn_scope("isolated"):
        assert controller.selected_backend_name() == "isolated"
    with browser_turn_scope("personal"):
        assert controller.invoke("get_browser_state", {})["backend"] == "personal"
    assert controller.selected_backend_name() == "isolated"
    assert [call[0] for call in personal.calls] == ["get_browser_state"]


def test_controller_never_falls_back_after_personal_blocker():
    isolated = RecordingBackend("isolated")
    personal = RecordingBackend(
        "personal",
        {"ok": False, "error": {"code": "no_authorized_tab"}},
    )
    controller = BrowserController(isolated=isolated, personal=personal)
    with browser_turn_scope("personal"):
        result = controller.invoke("get_browser_state", {})
    assert result["error"]["code"] == "no_authorized_tab"
    assert isolated.calls == []


def test_cross_backend_references_fail_closed_before_isolated_dispatch():
    result = IsolatedBrowserBackend().invoke(
        "get_browser_state",
        {"state_ref": {"browser_session_id": "personal_aaaaaaaa"}},
    )
    assert result["error"]["code"] == "cross_backend_reference"
    assert _wrong_personal_reference(
        {"state_ref": {"browser_session_id": "browser_aaaaaaaa"}},
        _target(),
    )


def test_personal_target_is_pinned_and_never_switches_mid_turn(monkeypatch):
    monkeypatch.setattr(
        "osia.config.load_settings",
        lambda: {
            "personal_browser_enabled": True,
            "personal_browser_preference": "automatic",
            "personal_browser_instance_hint": "",
        },
    )
    client = _client()
    first = _target()
    client.targets[first["target_handle"]] = first
    broker = _broker_without_listener(client)
    with browser_turn_scope("personal"):
        selected_client, selected, error = broker._select_target()
        assert (selected_client, selected, error) == (client, first, "")
        binding = current_browser_turn_binding()
        assert binding.personal_target_handle == first["target_handle"]
        client.targets.pop(first["target_handle"])
        second = _target("target:bbbbbbbb")
        client.targets[second["target_handle"]] = second
        assert broker._select_target() == (None, None, "authorization_revoked")


def test_multiple_targets_require_an_explicit_exact_hint(monkeypatch):
    settings = {
        "personal_browser_enabled": True,
        "personal_browser_preference": "automatic",
        "personal_browser_instance_hint": "",
    }
    monkeypatch.setattr("osia.config.load_settings", lambda: settings)
    client = _client()
    first = _target()
    second = _target("target:bbbbbbbb")
    client.targets = {first["target_handle"]: first, second["target_handle"]: second}
    broker = _broker_without_listener(client)
    with browser_turn_scope("personal"):
        assert broker._select_target() == (
            None, None, "multiple_sessions_selection_required",
        )
    settings["personal_browser_instance_hint"] = second["target_handle"]
    with browser_turn_scope("personal"):
        assert broker._select_target()[1]["target_handle"] == second["target_handle"]
    settings["personal_browser_instance_hint"] = "target:cccccccc"
    with browser_turn_scope("personal"):
        assert broker._select_target() == (None, None, "authorization_revoked")


def _request(**changes):
    value = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "message_type": "request",
        "request_id": "request:aaaaaaaa",
        "connection_id": "connection:aaaaaaaa",
        "target_handle": "target:aaaaaaaa",
        "operation": "get_browser_state",
        "parameters": {},
        "deadline_ms": int(time.time() * 1000) + 5_000,
    }
    value.update(changes)
    return value


def test_protocol_is_strict_versioned_bounded_and_rejects_unknown_operations():
    request = _request()
    assert protocol.decode_message(protocol.encode_message(request)) == request
    for changes, code in (
        ({"protocol_version": "2.0"}, "protocol_mismatch"),
        ({"operation": "syntactically_valid"}, "unknown_operation"),
        ({"extra": True}, "unknown_fields"),
        ({"deadline_ms": int(time.time() * 1000) - 5_000}, "invalid_deadline"),
    ):
        with pytest.raises(protocol.ProtocolError) as rejected:
            protocol.validate_message(_request(**changes))
        assert rejected.value.code == code
    with pytest.raises(protocol.ProtocolError) as oversized:
        protocol.decode_message(b"x" * (protocol.MAX_ENVELOPE_BYTES + 1))
    assert oversized.value.code == "message_too_large"


def test_protocol_origin_negotiation_cancellation_and_correlation():
    hello = {
        "protocol_version": "1.0",
        "message_type": "hello",
        "protocol_min": "1.0",
        "protocol_max": "1.0",
        "connection_id": "connection:aaaaaaaa",
        "install_instance_id": "install_aaaaaaaa",
        "browser_session_id": "personal_aaaaaaaa",
        "browser_family": "chrome",
        "caller_origin": f"chrome-extension://{DEVELOPMENT_EXTENSION_ID}/",
        "capabilities": ["get_browser_state"],
    }
    assert protocol.validate_message(hello)["caller_origin"] == hello["caller_origin"]
    with pytest.raises(protocol.ProtocolError) as foreign:
        protocol.validate_message(dict(hello, caller_origin="https://example.test/"))
    assert foreign.value.code == "origin_not_allowed"
    cancel = {
        "protocol_version": "1.0", "message_type": "cancel",
        "request_id": "request:aaaaaaaa",
        "connection_id": "connection:aaaaaaaa",
    }
    assert protocol.validate_message(cancel)["message_type"] == "cancel"

    owner = _client()
    other = _client("connection:bbbbbbbb", "personal_bbbbbbbb")
    broker = _broker_without_listener(owner)
    pending = _PendingRequest(
        connection_id=owner.connection_id,
        target_handle="target:aaaaaaaa",
    )
    broker._pending["request:aaaaaaaa"] = pending
    broker._handle_client_message(other, {
        "protocol_version": "1.0", "message_type": "response",
        "request_id": "request:aaaaaaaa", "connection_id": other.connection_id,
        "status": "ok", "error": None, "result": {"ok": True},
    })
    assert not pending.event.is_set()


def test_cross_process_control_protocol_is_strict_and_bounded():
    request = {
        "protocol_version": "1.0",
        "message_type": "control_request",
        "request_id": "control:aaaaaaaa",
        "control_operation": "invoke",
        "browser_operation": "get_browser_state",
        "arguments": {},
        "turn_binding": {
            "personal_connection_id": "",
            "personal_session_id": "",
            "personal_target_handle": "",
        },
    }
    assert protocol.decode_message(protocol.encode_message(request)) == request
    for changes in (
        {"control_operation": "switch_backend"},
        {"browser_operation": "unknown_but_valid"},
        {"unexpected": True},
    ):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_message({**request, **changes})
    status = {
        **request,
        "control_operation": "status",
        "browser_operation": None,
        "turn_binding": None,
    }
    assert protocol.validate_message(status)["control_operation"] == "status"
    progress = {
        "protocol_version": "1.0",
        "message_type": "control_progress",
        "request_id": "control:aaaaaaaa",
        "phase": "waiting",
    }
    assert protocol.validate_message(progress)["phase"] == "waiting"


def test_native_host_sends_hello_before_starting_duplex_reader(monkeypatch):
    import osia.personal_browser.native_host as native_host

    origin = f"chrome-extension://{DEVELOPMENT_EXTENSION_ID}/"
    hello = {
        "message_type": "hello",
        "caller_origin": origin,
    }
    messages = iter((hello, None))
    events = []

    class DeferredThread:
        def __init__(self, *, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            events.append("reader_started")

    monkeypatch.setattr(native_host, "_connect_pipe", lambda: object())
    monkeypatch.setattr(
        native_host,
        "read_framed",
        lambda _stream, *, native_endian: next(messages),
    )
    monkeypatch.setattr(
        native_host,
        "_pipe_write",
        lambda _pipe, message, _lock: events.append(("write", message)),
    )
    monkeypatch.setattr(native_host.threading, "Thread", DeferredThread)
    assert native_host.run(origin) == 0
    assert events == [("write", hello), "reader_started"]


def test_control_dispatch_uses_only_local_owner_methods(monkeypatch):
    broker = _broker_without_listener()
    monkeypatch.setattr(broker, "_status_local", lambda: {"state": "ready"})
    monkeypatch.setattr(broker, "_revoke_all_local", lambda: {"revoked": 1})
    monkeypatch.setattr(
        broker, "_invoke_local",
        lambda operation, arguments, *, progress_callback=None: {
            "operation": operation, "arguments": arguments,
        },
    )
    assert broker._dispatch_control_request({"control_operation": "status"}) == {
        "state": "ready",
    }
    assert broker._dispatch_control_request({"control_operation": "revoke"}) == {
        "revoked": 1,
    }
    assert broker._dispatch_control_request({
        "control_operation": "invoke",
        "browser_operation": "get_browser_state",
        "arguments": {"exact": True},
        "turn_binding": {
            "personal_connection_id": "",
            "personal_session_id": "",
            "personal_target_handle": "",
        },
    }) == {
        "control_result": {
            "operation": "get_browser_state", "arguments": {"exact": True},
        },
        "turn_binding": {
            "personal_connection_id": "",
            "personal_session_id": "",
            "personal_target_handle": "",
        },
    }


def test_effect_loss_maps_before_dispatch_and_after_dispatch_without_replay():
    before = _effect_error("disconnected", {}, dispatched=False)
    assert before["receipt"]["dispatch_state"] == "not_dispatched"
    assert before["receipt"]["outcome"] == "confirmed_failure_before_effect"
    unknown = _effect_error("outcome_unknown", {}, dispatched=True)
    assert unknown["receipt"]["dispatch_state"] == "effect_unknown"
    assert unknown["receipt"]["outcome"] == "outcome_unknown"
    assert unknown["receipt"]["evidence"] == [{"connection_lost": True}]


def test_revoke_is_acknowledged_or_forces_fail_closed_disconnect(monkeypatch):
    import osia.personal_browser.broker as broker_module

    client = _client()
    client.targets["target:aaaaaaaa"] = _target()
    broker = _broker_without_listener(client)

    def acknowledge(message):
        broker._handle_client_message(client, {
            "protocol_version": "1.0",
            "message_type": "response",
            "request_id": message["request_id"],
            "connection_id": client.connection_id,
            "status": "ok",
            "error": None,
            "result": {"ok": True},
        })

    monkeypatch.setattr(client, "send", acknowledge)
    assert broker._revoke_all_local() == {
        "ok": True, "revoked": 1, "confirmed": 1,
    }

    client.targets["target:aaaaaaaa"] = _target()
    disconnected = []
    monkeypatch.setattr(
        client, "send", lambda _message: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        broker_module, "_close_pipe", lambda handle: disconnected.append(handle),
    )
    assert broker._revoke_all_local() == {
        "ok": True, "revoked": 1, "confirmed": 0,
    }
    assert disconnected == [client.handle]


def test_manifest_has_only_approved_mv3_permissions_and_deterministic_dev_id():
    manifest = json.loads((EXTENSION / "manifest.json").read_text("utf-8"))
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {
        "activeTab", "scripting", "nativeMessaging", "storage",
    }
    assert manifest["incognito"] == "not_allowed"
    assert "host_permissions" not in manifest
    assert "content_scripts" not in manifest
    key = base64.b64decode(manifest["key"])
    digest = hashlib.sha256(key).digest()[:16]
    derived = "".join(
        chr(ord("a") + nibble)
        for byte in digest
        for nibble in (byte >> 4, byte & 0x0F)
    )
    assert derived == DEVELOPMENT_EXTENSION_ID
    assert allowed_origins(development=True) == [
        f"chrome-extension://{DEVELOPMENT_EXTENSION_ID}/"
    ]


def test_extension_authorization_revocation_privacy_and_sensitive_guards_are_explicit():
    worker = (EXTENSION / "service_worker.js").read_text("utf-8")
    agent = (EXTENSION / "content_agent.js").read_text("utf-8")
    assert "chrome.action.onClicked.addListener" in worker
    assert "chrome.runtime.onStartup.addListener" in worker
    assert "chrome.runtime.onInstalled.addListener" in worker
    assert "page.origin !== record.origin" in worker
    assert "challenge.document_nonce !== record.document_nonce" in worker
    assert "existing.tab_id === tab.id" in worker
    assert "record.target_handle" in worker
    assert "accounts.google.com" in worker
    assert "chromewebstore.google.com" in worker
    assert "manual_intervention_required" in worker
    assert "chrome.cookies" not in worker
    assert "chrome.history" not in worker
    assert "chrome.identity" not in worker
    assert "element.value" not in agent[:agent.index("function fillField")]
    assert "type === \"password\"" in agent
    assert "one-time-code" in agent


def test_personal_browser_uses_same_user_pipe_and_no_public_transport():
    broker = (ROOT / "osia" / "personal_browser" / "broker.py").read_text("utf-8")
    native = (ROOT / "osia" / "personal_browser" / "native_host.py").read_text("utf-8")
    assert "CreateNamedPipe" in broker
    assert "_same_user_security_attributes()" in broker
    assert "ImpersonateNamedPipeClient" in broker
    assert "current_user_sid()" in broker
    combined = broker + native
    assert "socket.socket" not in combined
    assert "localhost" not in combined
    assert "http://" not in combined
    assert "websocket" not in combined.lower()
    assert 'f"[OSIA NativeHost] {code[:64]}' in native
    assert "os._exit(0)" in native


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ownership gate")
def test_windows_broker_owner_survives_creator_thread_exit(monkeypatch):
    import secrets

    import osia.personal_browser.broker as broker_module

    suffix = secrets.token_hex(8)
    monkeypatch.setattr(
        broker_module,
        "_owner_mutex_name",
        lambda: rf"Local\space.osia.wor165.owner.{suffix}",
    )
    monkeypatch.setattr(
        broker_module,
        "_pipe_name",
        lambda: rf"\\.\pipe\space.osia.wor165.owner.{suffix}",
    )
    owner_box = []
    creator = threading.Thread(
        target=lambda: owner_box.append(PersonalBrowserBroker()),
    )
    creator.start()
    creator.join(3)
    assert not creator.is_alive()
    owner = owner_box[0]
    challenger = PersonalBrowserBroker()
    try:
        assert owner._owns_listener is True
        assert challenger._owns_listener is False
        assert challenger.status() == {
            "backend": "personal",
            "state": "browser_stopped",
            "targets": [],
            "capabilities": [],
        }
        owner.close()
        assert challenger._try_promote_to_owner() is True
    finally:
        owner.close()
        challenger.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows turn-binding gate")
def test_windows_proxy_returns_and_reuses_exact_turn_binding(monkeypatch):
    import secrets

    import osia.personal_browser.broker as broker_module

    suffix = secrets.token_hex(8)
    monkeypatch.setattr(
        broker_module,
        "_owner_mutex_name",
        lambda: rf"Local\space.osia.wor165.binding.{suffix}",
    )
    monkeypatch.setattr(
        broker_module,
        "_pipe_name",
        lambda: rf"\\.\pipe\space.osia.wor165.binding.{suffix}",
    )
    owner = PersonalBrowserBroker()
    proxy = PersonalBrowserBroker()
    seen = []

    progress_events = []
    proxy.set_progress_callback(progress_events.append)

    def invoke(_operation, _arguments, *, progress_callback=None):
        binding = current_browser_turn_binding()
        seen.append((
            binding.personal_connection_id,
            binding.personal_session_id,
            binding.personal_target_handle,
        ))
        if not binding.personal_connection_id:
            binding.personal_connection_id = "connection:aaaaaaaa"
            binding.personal_session_id = "personal_aaaaaaaa"
            binding.personal_target_handle = "target:aaaaaaaa"
        if callable(progress_callback):
            progress_callback({"phase": "waiting"})
        return {"ok": True}

    monkeypatch.setattr(owner, "_invoke_local", invoke)
    try:
        with browser_turn_scope("personal") as binding:
            assert proxy.invoke("get_browser_state", {}) == {"ok": True}
            assert (
                binding.personal_connection_id,
                binding.personal_session_id,
                binding.personal_target_handle,
            ) == (
                "connection:aaaaaaaa",
                "personal_aaaaaaaa",
                "target:aaaaaaaa",
            )
            assert proxy.invoke("get_browser_state", {}) == {"ok": True}
        assert seen == [
            ("", "", ""),
            (
                "connection:aaaaaaaa",
                "personal_aaaaaaaa",
                "target:aaaaaaaa",
            ),
        ]
        assert progress_events == [
            {"phase": "waiting"},
            {"phase": "waiting"},
        ]
    finally:
        proxy.close()
        owner.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows duplex-pipe gate")
def test_windows_broker_can_dispatch_while_client_reader_is_active():
    import secrets

    import pywintypes
    import win32con
    import win32file
    import win32pipe

    from osia.personal_browser.broker import (
        _read_pipe_message,
        _same_user_security_attributes,
        _write_pipe_message,
    )

    pipe_name = rf"\\.\pipe\space.osia.wor165.duplex.{secrets.token_hex(8)}"
    broker = _broker_without_listener()
    ready = threading.Event()

    def serve():
        handle = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT,
            1,
            protocol.MAX_ENVELOPE_BYTES + 4,
            protocol.MAX_ENVELOPE_BYTES + 4,
            0,
            _same_user_security_attributes(),
        )
        ready.set()
        try:
            win32pipe.ConnectNamedPipe(handle, None)
        except pywintypes.error as error:
            if error.winerror != 535:
                raise
        broker._serve_windows_client(handle)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    assert ready.wait(3)
    client_handle = win32file.CreateFile(
        pipe_name,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        0,
        None,
        win32con.OPEN_EXISTING,
        0,
        None,
    )
    hello = {
        "protocol_version": "1.0",
        "message_type": "hello",
        "protocol_min": "1.0",
        "protocol_max": "1.0",
        "connection_id": "connection:aaaaaaaa",
        "install_instance_id": "install_aaaaaaaa",
        "browser_session_id": "personal_aaaaaaaa",
        "browser_family": "chrome",
        "caller_origin": f"chrome-extension://{DEVELOPMENT_EXTENSION_ID}/",
        "capabilities": ["get_browser_state"],
    }
    try:
        _write_pipe_message(client_handle, hello)
        deadline = time.monotonic() + 3
        while not broker._clients and time.monotonic() < deadline:
            time.sleep(0.01)
        assert set(broker._clients) == {"connection:aaaaaaaa"}
        send = threading.Thread(
            target=lambda: broker._clients["connection:aaaaaaaa"].send(
                protocol.request_message(
                    request_id="request:aaaaaaaa",
                    connection_id="connection:aaaaaaaa",
                    target_handle="target:aaaaaaaa",
                    operation="get_browser_state",
                    parameters={},
                    timeout_seconds=2,
                )
            ),
            daemon=True,
        )
        send.start()
        request = _read_pipe_message(client_handle)
        send.join(2)
        assert not send.is_alive()
        assert request and request["operation"] == "get_browser_state"
    finally:
        win32file.CloseHandle(client_handle)
        broker._closed = True
        server.join(3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe gate")
def test_windows_named_pipe_accepts_only_the_current_user():
    import secrets

    import pywintypes
    import win32con
    import win32file
    import win32pipe

    from osia.personal_browser.broker import (
        _client_is_current_user,
        _same_user_security_attributes,
    )

    pipe_name = rf"\\.\pipe\space.osia.wor165.{secrets.token_hex(8)}"
    ready = threading.Event()
    result = []

    def serve():
        handle = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE
            | win32pipe.PIPE_READMODE_BYTE
            | win32pipe.PIPE_WAIT,
            1,
            4096,
            4096,
            0,
            _same_user_security_attributes(),
        )
        try:
            ready.set()
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as error:
                if error.winerror != 535:
                    raise
            _status, first_byte = win32file.ReadFile(handle, 1)
            assert first_byte == b"x"
            result.append(_client_is_current_user(handle))
        finally:
            win32file.CloseHandle(handle)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    assert ready.wait(3)
    client = win32file.CreateFile(
        pipe_name,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        0,
        None,
        win32con.OPEN_EXISTING,
        0,
        None,
    )
    try:
        win32file.WriteFile(client, b"x")
        server.join(3)
        assert not server.is_alive()
        assert result == [True]
    finally:
        win32file.CloseHandle(client)


def test_settings_are_setup_only_and_production_ids_fail_closed(monkeypatch):
    from osia.config import DEFAULT_SETTINGS

    spec = importlib.util.spec_from_file_location(
        "wor165_build_windows",
        ROOT / "packaging" / "windows" / "build_windows.py",
    )
    assert spec and spec.loader
    build_windows = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_windows)

    assert DEFAULT_SETTINGS["personal_browser_enabled"] is False
    assert DEFAULT_SETTINGS["personal_browser_preference"] == "automatic"
    assert DEFAULT_SETTINGS["personal_browser_instance_hint"] == ""
    assert "browser_mode" not in DEFAULT_SETTINGS
    monkeypatch.delenv("OSIA_CHROME_EXTENSION_ID", raising=False)
    monkeypatch.delenv("OSIA_EDGE_EXTENSION_ID", raising=False)
    with pytest.raises(RuntimeError, match="OSIA_CHROME_EXTENSION_ID"):
        build_windows._stage_personal_browser_metadata()
    monkeypatch.setattr(
        "osia.personal_browser.registration._metadata",
        lambda: {"chrome_store_id": "", "edge_store_id": ""},
    )
    assert allowed_origins(development=False) == []


def test_registration_and_packaging_are_explicit_and_ownership_safe():
    registration = (
        ROOT / "osia" / "personal_browser" / "registration_windows.py"
    ).read_text("utf-8")
    launcher = (ROOT / "packaging" / "windows" / "launcher.py").read_text("utf-8")
    installer = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "HKEY_CURRENT_USER" in registration
    assert "marker.get(\"owner\") == HOST_NAME" in registration
    assert "os.path.normcase(value) == os.path.normcase(str(_MANIFEST))" in registration
    assert "repair_owned_registration" in launcher
    assert "--osia-remove-personal-browser-registration" in launcher
    assert "--osia-remove-personal-browser-registration" in installer
    assert "Personal Browser" not in installer.partition("[Run]")[2].partition("[UninstallRun]")[0]
