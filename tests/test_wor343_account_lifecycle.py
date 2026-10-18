"""Process and authentication boundaries, without launching Codex or logging in."""

import io
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from osia.chatgpt_account.app_server_client import AppServerClient, AppServerError, _PendingRequest
from tools.test_chatgpt_account_client import FakeFactory, _base_handler


@pytest.fixture
def runtime(tmp_path):
    factory = FakeFactory(_base_handler)
    client = AppServerClient(
        base_dir=tmp_path / "runtime",
        process_factory=factory,
        runtime_executable=tmp_path / "codex.exe",
        runtime_version_override="codex-cli 0.144.1",
        request_timeout=1,
        compact_logs_on_startup=False,
    )
    yield client, factory
    client.shutdown()


@pytest.mark.parametrize("message", [
    {"id": 7, "result": {"account": {"type": "chatgpt"}}},
    {"id": 8, "method": "item/tool/call", "params": {}},
    {"method": "account/login/completed", "params": {"loginId": "old", "success": True}},
    {"method": "item/agentMessage/delta", "params": {"delta": "obsolete"}},
])
def test_replaced_process_cannot_supply_responses_requests_or_notifications(tmp_path, message):
    client = AppServerClient(base_dir=tmp_path)
    retired = SimpleNamespace(stdout=io.StringIO(json.dumps(message) + "\n"))
    client._process = object()
    pending = _PendingRequest()
    client._pending["7"] = pending
    dispatched = []
    client._handle_server_request = lambda value: dispatched.append(value)
    client._dispatch_notification = lambda *values: dispatched.append(values)
    client._reader_loop(retired)
    assert pending.response.empty()
    assert dispatched == []


def test_retired_process_exit_and_stderr_cannot_poison_new_runtime(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client._process = object()
    retired = SimpleNamespace(wait=lambda: 1, stderr=io.StringIO("ERROR obsolete\n"))
    client._wait_loop(retired)
    client._stderr_loop(retired)
    assert client._crash_times == []
    assert client._stderr_categories == {}


def test_current_process_still_delivers_responses(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    process = SimpleNamespace(stdout=io.StringIO('{"id":7,"result":{"ok":true}}\n'))
    client._process = process
    pending = _PendingRequest()
    client._pending["7"] = pending
    client._reader_loop(process)
    assert pending.response.get_nowait()["result"] == {"ok": True}


def test_logout_cancels_pending_login_and_releases_waiter(runtime):
    client, factory = runtime
    login = client.begin_login()
    waiter = client._login_events[login["loginId"]]
    client.logout()
    assert waiter[0].is_set()
    assert waiter[1]["cancelled"] is True
    methods = [item.get("method") for item in factory.processes[0].received]
    assert methods.index("account/login/cancel") < methods.index("account/logout")
    assert client._active_login is None


@pytest.mark.parametrize("reason", ["process_crash", "protocol_invalid", "shutdown"])
def test_runtime_failure_releases_login_with_typed_reason(runtime, reason):
    client, _ = runtime
    login = client.begin_login()
    waiter = client._login_events[login["loginId"]]
    # Capture the waiter before failure just as wait_for_login does.
    entered = threading.Event()
    original_wait = waiter[0].wait
    waiter[0].wait = lambda timeout: (entered.set(), original_wait(timeout))[1]
    result = queue.Queue()

    def wait():
        try:
            client.wait_for_login(login["loginId"], timeout=1)
        except AppServerError as error:
            result.put(error.category)

    thread = threading.Thread(target=wait)
    thread.start()
    assert entered.wait(1)
    client._fail_all(reason)
    thread.join(2)
    assert not thread.is_alive()
    assert result.get_nowait() == ("login_cancelled" if reason == "shutdown" else reason)
    assert client._active_login is None
    # Failure of one attempt cannot prevent a genuinely new login.
    assert client.begin_login()["loginId"]
