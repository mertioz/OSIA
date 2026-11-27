"""A slow provider metadata read cannot hold the PC config reply behind it."""
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

from osia import config, conversations, mobile_model_catalogue, scheduled_tasks
from osia.chatgpt_account import provider
from osia.durable_transport import DurableTransportStore
from osia.server import server


@pytest.mark.parametrize("request_kind,reply", [
    ("get_models", "models_list"),
    ("get_chatgpt_account", "chatgpt_account_state"),
])
def test_config_overtakes_pending_provider_read_without_losing_reply(tmp_path, monkeypatch, request_kind, reply):
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-only")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server, "ATTACHMENT_UPLOAD_DIR", tmp_path / "attachments")
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_build_config_snapshot", lambda: {"time_format": "12h"})
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(mobile_model_catalogue, "request_remote_refresh", lambda callback: callback())
    monkeypatch.setattr("osia.models.get_local_models", lambda: [])
    monkeypatch.setattr(mobile_model_catalogue, "account_snapshot", lambda **kwargs: {})

    def slow_read(**kwargs):
        started.set()
        assert release.wait(5), "test metadata read was not released"
        return {"version": 1, "providers": [], "models": [], "current": None,
                "currentProvider": None, "account": {}}

    monkeypatch.setattr(mobile_model_catalogue,
                        "build_catalogue" if request_kind == "get_models" else "account_snapshot", slow_read)
    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    timer = threading.Timer(2, release.set)
    try:
        with TestClient(app, client=("127.0.0.1", 54321)) as client:
            with client.websocket_connect("/ws") as socket:
                socket.send_json({"type": "auth", "token": "test-only", "event_cursors": {}})
                while socket.receive_json().get("type") != "conversations":
                    pass
                socket.send_json({"type": request_kind})
                assert started.wait(2)
                timer.start()  # Old serialized code safely fails instead of hanging the test.
                socket.send_json({"type": "get_config"})
                while True:
                    frame = socket.receive_json()
                    if frame.get("type") in {"config", reply}:
                        break
                assert frame["type"] == "config"
                assert frame["config"]["time_format"] == "12h"
                assert not release.is_set()
                release.set()
                while socket.receive_json().get("type") != reply:
                    pass
    finally:
        release.set()
        timer.cancel()


def test_first_models_and_config_arrive_while_account_is_still_loading(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    slow_reads = []
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-only")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_build_config_snapshot", lambda: {"time_format": "24h"})
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(mobile_model_catalogue, "request_remote_refresh", lambda callback: callback())
    monkeypatch.setattr("osia.models.get_local_models", lambda: [])
    def account_read(**kwargs):
        slow_reads.append(kwargs)
        started.set()
        assert release.wait(5)
    def catalogue(**kwargs):
        assert kwargs["cached_only"]
        model = {"id": "fixture", "providerId": "local"}
        return {"version": 1, "providers": [], "models": [model], "current": "fixture",
                "currentProvider": "local", "account": {}}
    monkeypatch.setattr(mobile_model_catalogue, "account_snapshot", account_read)
    monkeypatch.setattr(mobile_model_catalogue, "build_catalogue", catalogue)
    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    with TestClient(app, client=("127.0.0.1", 54321)) as client:
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"type": "auth", "token": "test-only", "event_cursors": {}})
            while socket.receive_json().get("type") != "conversations":
                pass
            try:
                socket.send_json({"type": "get_models"})
                assert started.wait(3)
                socket.send_json({"type": "get_models"})
                socket.send_json({"type": "get_config"})
                first = set()
                while first != {"config", "models_list"}:
                    frame = socket.receive_json()
                    if frame["type"] == "models_list":
                        assert frame["catalogue"]["refreshing"] and frame["models"][0]["id"] == "fixture"
                    if frame["type"] in {"config", "models_list"}:
                        first.add(frame["type"])
            finally:
                release.set()
            while True:
                frame = socket.receive_json()
                if frame["type"] == "models_list" and not frame["catalogue"]["refreshing"]:
                    break
            assert slow_reads == [{"refresh_models": True, "include_usage": False}]


def test_cached_inventory_skips_live_probes_and_invalidates_external_account_changes(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    revision = [1]
    client = SimpleNamespace(account_generation=1, _read_auth_storage_revision=lambda: revision[0])
    account = SimpleNamespace(client=client, status=Mock(return_value={
        "runtimeAvailable": True, "available": True, "connected": True,
        "account": {"email": "fixture@example.test"}, "models": [{"id": "fixture-account"}],
        "selectedModel": "fixture-account",
    }), rate_limits=Mock(side_effect=AssertionError("quota on catalogue path")))
    monkeypatch.setattr(provider, "get_account_provider", lambda: account)
    monkeypatch.setattr(mobile_model_catalogue, "_last_account_status", None)
    monkeypatch.setattr(mobile_model_catalogue, "get_all_models",
                        Mock(side_effect=AssertionError("live local discovery")))
    monkeypatch.setattr(mobile_model_catalogue, "get_all_models_cached", lambda **kwargs: [])
    monkeypatch.setattr(mobile_model_catalogue, "configured_api_providers", lambda keys: set())
    monkeypatch.setattr(mobile_model_catalogue, "load_api_keys", lambda: {})
    assert mobile_model_catalogue.build_catalogue(cached_only=True)["account"] == {}
    mobile_model_catalogue.account_snapshot(include_usage=False)
    assert mobile_model_catalogue.build_catalogue(cached_only=True)["account"]["connected"]
    revision[0] += 1
    assert mobile_model_catalogue.build_catalogue(cached_only=True)["account"] == {}
    account.status.assert_called_once()
    account.rate_limits.assert_not_called()


def test_device_sign_in_survives_mobile_reconnect_and_logout(tmp_path, monkeypatch):
    finish_login = threading.Event()
    login_ended = threading.Event()
    connected = False
    calls = []

    class Account:
        def status(self, *, refresh_models):
            return {
                "connected": connected,
                "runtimeAvailable": True,
                "error": "" if connected else "disconnected",
            }

        def begin_device_code_login(self):
            calls.append("start")
            return {
                "loginId": "fixture",
                "verificationUrl": "https://auth.openai.com/device",
                "userCode": "TEST-CODE",
            }

        def wait_for_login(self, login_id, timeout):
            nonlocal connected
            assert login_id == "fixture"
            assert finish_login.wait(5)
            connected = True

        def rate_limits(self):
            return {}

        def cancel_login(self):
            calls.append("cancel")

        def logout(self):
            nonlocal connected
            connected = False
            provider.notify_account_changed()

    owner = Account()
    monkeypatch.setattr(provider, "get_account_provider", lambda: owner)
    monkeypatch.setattr(mobile_model_catalogue, "_login_generation", 0)
    monkeypatch.setattr(mobile_model_catalogue, "_login_connecting", False)
    monkeypatch.setattr(mobile_model_catalogue, "_login_ephemeral", {})
    monkeypatch.setattr(mobile_model_catalogue, "_login_error", "")
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-only")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server, "ATTACHMENT_UPLOAD_DIR", tmp_path / "attachments")
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(mobile_model_catalogue, "request_remote_refresh", lambda callback: None)
    monkeypatch.setattr(mobile_model_catalogue, "build_catalogue", lambda **kwargs: {
        "version": 1, "providers": [], "models": [], "current": None,
        "currentProvider": None, "account": {},
    })

    def auth(socket):
        socket.send_json({"type": "auth", "token": "test-only", "event_cursors": {}})
        while socket.receive_json().get("type") != "conversations":
            pass

    def read_account(socket, *, require_code=False, expected_connected=None):
        for _ in range(20):
            frame = socket.receive_json()
            if frame.get("type") != "chatgpt_account_state":
                continue
            state = frame["account"]
            if require_code and not state.get("login"):
                continue
            if expected_connected is not None and state["connected"] != expected_connected:
                continue
            return state
        raise AssertionError("Missing account state")

    unsubscribe = provider.add_account_change_listener(login_ended.set)
    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    try:
        with TestClient(app, client=("127.0.0.1", 54321)) as client:
            with client.websocket_connect("/ws") as socket:
                auth(socket)
                socket.send_json({"type": "chatgpt_account_login"})
                assert read_account(socket, require_code=True)["login"]["userCode"] == "TEST-CODE"
            with client.websocket_connect("/ws") as reconnected:
                auth(reconnected)
                reconnected.send_json({"type": "get_chatgpt_account"})
                pending = read_account(reconnected)
                assert pending["connecting"] and "login" not in pending
                reconnected.send_json({"type": "chatgpt_account_login"})
                assert read_account(reconnected, require_code=True)["login"]["userCode"] == "TEST-CODE"
                assert calls == ["start"]
                finish_login.set()
                assert login_ended.wait(3)
                signed_in = read_account(reconnected, expected_connected=True)
                assert not signed_in["connecting"] and "login" not in signed_in
                reconnected.send_json({"type": "chatgpt_account_logout"})
                signed_out = read_account(reconnected, expected_connected=False)
                assert not signed_out["connecting"] and not connected
    finally:
        finish_login.set()
        unsubscribe()


def test_stale_mobile_cron_write_returns_fresh_config_without_deleting_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "_ensure_config_dir", lambda: None)
    monkeypatch.setattr(server, "MOBILE_TOKEN", "test-only")
    monkeypatch.setattr(server, "_DURABLE_TRANSPORT", DurableTransportStore(tmp_path / "transport"))
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr(server, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(server, "ATTACHMENT_UPLOAD_DIR", tmp_path / "attachments")
    monkeypatch.setattr(server, "_WS_CLIENTS", set())
    monkeypatch.setattr(server, "_pending_cron_notifications", lambda: [])
    monkeypatch.setattr(mobile_model_catalogue, "request_remote_refresh", lambda callback: None)

    app = Starlette(routes=[WebSocketRoute("/ws", server.websocket_endpoint)])
    with TestClient(app, client=("127.0.0.1", 54321)) as client:
        with client.websocket_connect("/ws") as socket:
            socket.send_json({"type": "auth", "token": "test-only", "event_cursors": {}})
            while socket.receive_json().get("type") != "conversations":
                pass
            socket.send_json({"type": "get_config"})
            while (frame := socket.receive_json()).get("type") != "config":
                pass
            original = frame["config"]
            assert original["cron_jobs"] == []
            assert original["cron_jobs_revision"]
            created = scheduled_tasks.create_scheduled_task(
                instruction="Return desktop status",
                schedule={"type": "interval", "every": 5, "unit": "minutes"},
                source="settings",
            )
            assert created["ok"]

            socket.send_json({
                "type": "set_setting",
                "key": "cron_jobs",
                "value": [],
                "expected_cron_jobs_revision": original["cron_jobs_revision"],
            })
            for _ in range(20):
                frame = socket.receive_json()
                assert frame.get("type") != "status", frame
                if frame.get("type") == "config" and frame["config"].get("cron_jobs_conflict"):
                    break
            else:
                pytest.fail("Missing cron conflict config")
            fresh = frame["config"]
            assert fresh["cron_jobs_revision"] != original["cron_jobs_revision"]
            assert [job["id"] for job in fresh["cron_jobs"]] == [created["task_id"]]
            assert fresh["cron_jobs"][0]["task_redacted"] is True
            assert [job["id"] for job in scheduled_tasks.scheduler_jobs_snapshot()] == [created["task_id"]]
