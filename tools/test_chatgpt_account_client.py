#!/usr/bin/env python3
"""Deterministic protocol/isolation tests for WOR-113."""

from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from osia.chatgpt_account.app_server_client import (
    AppServerClient, AppServerError, RuntimeUnavailableError,
)


class _QueueReader:
    def __init__(self):
        self.items = queue.Queue()

    def push(self, message: dict):
        self.items.put(json.dumps(message) + "\n")

    def close(self):
        self.items.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        item = self.items.get(timeout=5)
        if item is None:
            raise StopIteration
        return item


class _FakeStdin:
    def __init__(self, owner):
        self.owner = owner
        self.buffer = ""
        self.closed = False

    def write(self, raw):
        self.buffer += raw
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line:
                self.owner.receive(json.loads(line))

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, handler, command, kwargs):
        self.handler = handler
        self.command = command
        self.kwargs = kwargs
        self.stdout = _QueueReader()
        self.stderr = _QueueReader()
        self.stdin = _FakeStdin(self)
        self.returncode = None
        self.received = []
        self.done = threading.Event()

    def receive(self, message):
        self.received.append(message)
        self.handler(self, message)

    def emit(self, message):
        self.stdout.push(message)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.close()
        self.stderr.close()
        self.done.set()

    def kill(self):
        self.terminate()


class FakeFactory:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.processes = []

    def __call__(self, command, **kwargs):
        process = FakeProcess(self.handler, command, kwargs)
        self.calls.append((command, kwargs))
        self.processes.append(process)
        return process


def _base_handler(process, message):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        process.emit({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "account/read":
        process.emit(
            {"id": request_id, "result": {"account": None, "requiresOpenaiAuth": True}}
        )
    elif method == "account/login/start":
        if message.get("params", {}).get("type") == "chatgptDeviceCode":
            result = {
                "type": "chatgptDeviceCode",
                "loginId": "login-device-1",
                "verificationUrl": "https://auth.openai.com/device",
                "userCode": "SAFE-CODE",
            }
        else:
            result = {
                "type": "chatgpt", "loginId": "login-1",
                "authUrl": "https://chatgpt.com/auth/fake",
            }
        process.emit({"id": request_id, "result": result})
    elif method in {"account/login/cancel", "account/logout", "turn/interrupt"}:
        process.emit({"id": request_id, "result": {}})
    elif method == "model/list":
        process.emit(
            {"id": request_id, "result": {"data": [
                {
                    "id": "visible", "model": "visible", "displayName": "Visible",
                    "hidden": False, "isDefault": True,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Fast"}
                    ],
                    "inputModalities": ["text"], "description": "",
                },
                {
                    "id": "hidden", "model": "hidden", "displayName": "Hidden",
                    "hidden": True, "isDefault": False,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [], "inputModalities": ["text"],
                    "description": "",
                },
                {
                    "id": "image-only", "model": "image-only", "displayName": "Image",
                    "hidden": False, "isDefault": False,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [], "inputModalities": ["image"],
                    "description": "",
                },
            ], "nextCursor": None}}
        )


class AppServerClientTests(unittest.TestCase):
    def make_client(self, handler=_base_handler):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        factory = FakeFactory(handler)
        client = AppServerClient(
            base_dir=Path(temp.name) / "runtime",
            process_factory=factory,
            runtime_executable=Path(temp.name) / "codex.exe",
            runtime_version_override="codex-cli 0.144.1",
            startup_timeout=2,
            request_timeout=2,
        )
        self.addCleanup(client.shutdown)
        return client, factory

    def test_initialize_once_and_isolated_process(self):
        client, factory = self.make_client()
        client.health_check()
        client.health_check()
        self.assertEqual(len(factory.processes), 1)
        process = factory.processes[0]
        methods = [item.get("method") for item in process.received]
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(factory.calls[0][0][-3:], ["app-server", "--stdio", "--strict-config"])
        kwargs = factory.calls[0][1]
        self.assertEqual(Path(kwargs["cwd"]), client.workspace_dir)
        self.assertNotIn(str(Path.cwd()), str(client.workspace_dir))
        env = kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(client.home_dir))
        self.assertEqual(env["HOME"], str(client.home_dir))
        self.assertEqual(env["TEMP"], str(client.temp_dir))
        self.assertFalse(
            any(any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                for key in env)
        )
        config = (client.home_dir / "config.toml").read_text(encoding="utf-8")
        self.assertIn('web_search = "disabled"', config)
        self.assertIn('inherit = "none"', config)

    def test_disconnected_login_is_single_flight_and_cancel_is_correlated(self):
        client, factory = self.make_client()
        self.assertFalse(client.get_account_status()["connected"])
        first = client.begin_login()
        second = client.begin_login()
        self.assertEqual(first, second)
        client.cancel_login()
        process = factory.processes[0]
        starts = [m for m in process.received if m.get("method") == "account/login/start"]
        cancels = [m for m in process.received if m.get("method") == "account/login/cancel"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(cancels[0]["params"], {"loginId": "login-1"})

    def test_official_device_code_login_is_single_flight_and_cancelled(self):
        client, factory = self.make_client()
        first = client.begin_device_code_login()
        second = client.begin_device_code_login()
        self.assertEqual(first, second)
        self.assertEqual(first["userCode"], "SAFE-CODE")
        self.assertEqual(first["verificationUrl"], "https://auth.openai.com/device")
        client.cancel_login()
        process = factory.processes[0]
        starts = [m for m in process.received if m.get("method") == "account/login/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["params"], {"type": "chatgptDeviceCode"})

    def test_login_completion_and_logout_refresh_official_account_state(self):
        state = {"connected": False}

        def handler(process, message):
            if message.get("method") == "account/read":
                account = (
                    {"type": "chatgpt", "email": "safe@example.test", "planType": "plus"}
                    if state["connected"] else None
                )
                process.emit({"id": message["id"], "result": {
                    "account": account, "requiresOpenaiAuth": True}})
            elif message.get("method") == "account/login/start":
                _base_handler(process, message)
                state["connected"] = True
                process.emit({"method": "account/login/completed", "params": {
                    "loginId": "login-1", "success": True, "error": None}})
                process.emit({"method": "account/updated", "params": {
                    "authMode": "chatgpt", "planType": "plus"}})
            elif message.get("method") == "account/logout":
                state["connected"] = False
                _base_handler(process, message)
            else:
                _base_handler(process, message)

        client, _factory = self.make_client(handler)
        login = client.begin_login()
        status = client.wait_for_login(login["loginId"], timeout=2)
        self.assertTrue(status["connected"])
        self.assertEqual(status["account"]["planType"], "plus")
        client.logout()
        self.assertFalse(client.get_account_status()["connected"])

    def test_custom_configuration_fails_closed_before_process_start(self):
        client, factory = self.make_client()
        (client.home_dir / "plugins").mkdir(parents=True)
        with self.assertRaises(RuntimeUnavailableError) as caught:
            client.start()
        self.assertEqual(caught.exception.category, "isolation_failed")
        self.assertEqual(factory.calls, [])

    def test_repeated_crashes_disable_bounded_restart(self):
        client, factory = self.make_client()
        client._crash_times = [time.monotonic(), time.monotonic()]
        with self.assertRaises(RuntimeUnavailableError) as caught:
            client.start()
        self.assertEqual(caught.exception.category, "terminal_session_error")
        self.assertEqual(factory.calls, [])

    def test_model_list_filters_hidden_and_unusable(self):
        client, _factory = self.make_client()
        models = client.list_models()
        self.assertEqual([item["id"] for item in models], ["visible"])
        self.assertEqual(models[0]["supportedReasoningEfforts"][0]["id"], "low")

    def test_streamed_agent_text_and_textual_tool_call_are_accepted(self):
        def handler(process, message):
            if message.get("method") == "turn/start":
                rid = message["id"]
                process.emit({"id": rid, "result": {"turn": {"id": "turn-1"}}})
                process.emit({"method": "turn/started", "params": {
                    "threadId": "thread-1", "turn": {"id": "turn-1"}}})
                process.emit({"method": "item/agentMessage/delta", "params": {
                    "threadId": "thread-1", "turnId": "turn-1",
                    "delta": '<tool_call>{"tool":"read_file","path":"x"}</tool_call>'}})
                process.emit({"method": "turn/completed", "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"}}})
            else:
                _base_handler(process, message)

        client, _factory = self.make_client(handler)
        deltas = []
        result = client.run_turn(
            thread_id="thread-1", text="test", model="visible", on_delta=deltas.append
        )
        self.assertIn("<tool_call>", result.text)
        self.assertEqual(result.text, "".join(deltas))

    def test_native_approval_is_cancelled_and_turn_interrupted_without_fallback(self):
        def handler(process, message):
            if message.get("method") == "turn/start":
                rid = message["id"]
                process.emit({"id": rid, "result": {"turn": {"id": "turn-2"}}})
                process.emit({"method": "item/started", "params": {
                    "threadId": "thread-2", "turnId": "turn-2",
                    "item": {"id": "item-1", "type": "commandExecution"}}})
                process.emit({"id": 900, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": "thread-2", "turnId": "turn-2",
                                         "itemId": "item-1"}})
            elif message.get("id") == 900 and "result" in message:
                pass
            else:
                _base_handler(process, message)

        client, factory = self.make_client(handler)
        with self.assertRaises(AppServerError) as caught:
            client.run_turn(thread_id="thread-2", text="test", model="visible")
        self.assertTrue(caught.exception.native_action)
        self.assertFalse(caught.exception.allow_fallback)
        received = factory.processes[0].received
        approval = next(m for m in received if m.get("id") == 900 and "result" in m)
        self.assertEqual(approval["result"], {"decision": "cancel"})
        interrupt = next(m for m in received if m.get("method") == "turn/interrupt")
        self.assertEqual(interrupt["params"], {"threadId": "thread-2", "turnId": "turn-2"})

    def test_unknown_action_item_fails_closed(self):
        def handler(process, message):
            if message.get("method") == "turn/start":
                process.emit({"id": message["id"], "result": {"turn": {"id": "turn-x"}}})
                process.emit({"method": "item/started", "params": {
                    "threadId": "thread-x", "turnId": "turn-x",
                    "item": {"id": "item-x", "type": "futureNativeAction"}}})
            else:
                _base_handler(process, message)

        client, _factory = self.make_client(handler)
        with self.assertRaises(AppServerError) as caught:
            client.run_turn(thread_id="thread-x", text="test", model="visible")
        self.assertTrue(caught.exception.native_action)
        self.assertFalse(caught.exception.allow_fallback)

    def test_all_stable_native_action_categories_fail_closed(self):
        for item_type in (
            "fileChange", "mcpToolCall", "webSearch", "imageGeneration",
            "collabAgentToolCall", "subAgentActivity", "dynamicToolCall",
        ):
            with self.subTest(item_type=item_type):
                def handler(process, message, current=item_type):
                    if message.get("method") == "turn/start":
                        process.emit({"id": message["id"], "result": {"turn": {"id": "turn-a"}}})
                        process.emit({"method": "item/started", "params": {
                            "threadId": "thread-a", "turnId": "turn-a",
                            "item": {"id": "item-a", "type": current}}})
                    else:
                        _base_handler(process, message)

                client, _factory = self.make_client(handler)
                with self.assertRaises(AppServerError) as caught:
                    client.run_turn(thread_id="thread-a", text="test", model="visible")
                self.assertTrue(caught.exception.native_action)
                self.assertFalse(caught.exception.allow_fallback)


if __name__ == "__main__":
    unittest.main()
