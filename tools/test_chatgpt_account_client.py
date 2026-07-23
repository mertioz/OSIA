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
from unittest.mock import patch

from osia.chatgpt_account import app_server_client as client_module
from osia.chatgpt_account.app_server_client import (
    AppServerClient, AppServerError, RuntimeUnavailableError,
)
from osia.chatgpt_account.rate_limits import (
    merge_rate_limit_payload,
    quota_windows,
    sanitize_rate_limit_payload,
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

    def run_native_failure(
        self,
        *,
        item_type=None,
        approval_method=None,
        notification_method=None,
        delta=None,
        language="en",
        text="test",
    ):
        def handler(process, message):
            if message.get("method") == "turn/start":
                process.emit({
                    "id": message["id"],
                    "result": {"turn": {"id": "turn-native"}},
                })
                if delta:
                    process.emit({
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thread-native",
                            "turnId": "turn-native",
                            "delta": delta,
                        },
                    })
                if item_type:
                    process.emit({
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-native",
                            "turnId": "turn-native",
                            "item": {"id": "item-native", "type": item_type},
                        },
                    })
                if approval_method:
                    process.emit({
                        "id": 900,
                        "method": approval_method,
                        "params": {
                            "threadId": "thread-native",
                            "turnId": "turn-native",
                            "itemId": "item-native",
                        },
                    })
                if notification_method:
                    process.emit({
                        "method": notification_method,
                        "params": {
                            "threadId": "thread-native",
                            "turnId": "turn-native",
                        },
                    })
            elif message.get("id") == 900 and "result" in message:
                pass
            else:
                _base_handler(process, message)

        client, factory = self.make_client(handler)
        deltas = []
        with patch("osia.i18n.get_language", return_value=language):
            with self.assertRaises(AppServerError) as caught:
                client.run_turn(
                    thread_id="thread-native",
                    text=text,
                    model="visible",
                    on_delta=deltas.append,
                )
        return caught.exception, factory.processes[0].received, deltas

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

    def test_managed_runtime_is_primary_and_global_codex_is_not_required(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        managed = Path(temp.name) / "bundle" / "runtimes" / "codex" / "bin" / "codex.exe"
        managed.parent.mkdir(parents=True)
        managed.touch()
        client = AppServerClient(base_dir=Path(temp.name) / "profile")
        with (
            patch.object(client_module, "resource_path", return_value=managed),
            patch.object(client_module.shutil, "which", return_value=None),
            patch.object(
                client_module,
                "_probe_executable",
                side_effect=lambda path: (
                    "codex-cli 0.144.1" if Path(path) == managed else None
                ),
            ),
            patch.dict(os.environ, {"OSIA_CODEX_EXECUTABLE": ""}),
        ):
            executable, version = client._resolve_runtime()
        self.assertEqual(executable, managed)
        self.assertEqual(version, "codex-cli 0.144.1")

    def test_rejected_rate_limit_refresh_preserves_sparse_updated_snapshot(self):
        reads = 0

        def handler(process, message):
            nonlocal reads
            if message.get("method") != "account/rateLimits/read":
                _base_handler(process, message)
                return
            reads += 1
            if reads == 1:
                snapshot = {
                    "primary": {"usedPercent": 37, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 21, "windowDurationMins": 10080},
                }
                process.emit({"id": message["id"], "result": {
                    "rateLimits": snapshot,
                    "rateLimitsByLimitId": {"codex": snapshot},
                }})
            else:
                process.emit({"id": message["id"], "error": {
                    "code": -32000, "message": "sensitive upstream detail"}})

        client, _factory = self.make_client(handler)
        first = client.get_rate_limits()
        self.assertEqual(first["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"], 37)

        client._dispatch_notification("account/rateLimits/updated", {
            "rateLimits": {"primary": {"usedPercent": 0}},
        })
        preserved = client.get_rate_limits()
        self.assertEqual(preserved["rateLimits"]["primary"]["usedPercent"], 0)
        self.assertEqual(
            preserved["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"], 0
        )
        self.assertEqual(
            quota_windows(preserved),
            [
                {"label": "5 h", "remainingPercent": 100},
                {"label": "Weekly", "remainingPercent": 79},
            ],
        )
        self.assertNotIn("sensitive upstream detail", json.dumps(preserved))

    def test_update_received_during_read_is_merged_after_response(self):
        client, _factory = self.make_client()

        def request(_method, _params):
            client._dispatch_notification("account/rateLimits/updated", {
                "rateLimits": {"primary": {"usedPercent": 0}},
            })
            return {"rateLimits": {
                "primary": {"usedPercent": 48, "windowDurationMins": 300},
            }}

        client._request = request
        snapshot = client.get_rate_limits()
        self.assertEqual(snapshot["rateLimits"]["primary"], {
            "usedPercent": 0, "windowDurationMins": 300,
        })

    def test_primary_secondary_and_used_to_remaining(self):
        windows = quota_windows({"rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300},
            "secondary": {"usedPercent": 21, "windowDurationMins": 10080},
        }})
        self.assertEqual(windows, [
            {"label": "5 h", "remainingPercent": 100},
            {"label": "Weekly", "remainingPercent": 79},
        ])

    def test_multiple_internal_buckets_do_not_leak_into_ui_windows(self):
        payload = {
            "rateLimits": {
                "primary": {"usedPercent": 10, "windowDurationMins": 300},
                "secondary": {"usedPercent": 20, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"usedPercent": 10, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 20, "windowDurationMins": 10080},
                },
                "codex_extra": {
                    "primary": {"usedPercent": 30, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
                },
            },
        }
        safe = sanitize_rate_limit_payload(payload)
        self.assertEqual(set(safe["rateLimitsByLimitId"]), {"codex", "codex_extra"})
        self.assertEqual(quota_windows(payload), [
            {"label": "5 h", "remainingPercent": 90},
            {"label": "Weekly", "remainingPercent": 80},
        ])

    def test_missing_canonical_bucket_does_not_fabricate_ui_windows(self):
        payload = {"rateLimitsByLimitId": {"codex_extra": {
            "primary": {"usedPercent": 30, "windowDurationMins": 300},
        }}}
        self.assertIn("codex_extra", sanitize_rate_limit_payload(payload)["rateLimitsByLimitId"])
        self.assertEqual(quota_windows(payload), [])

    def test_sparse_window_reset_from_nonzero_to_zero(self):
        base = {"rateLimits": {
            "primary": {"usedPercent": 52, "windowDurationMins": 300},
        }}
        merged = merge_rate_limit_payload(
            base, {"rateLimits": {"primary": {"usedPercent": 0}}}
        )
        self.assertEqual(merged["rateLimits"]["primary"], {
            "usedPercent": 0, "windowDurationMins": 300,
        })
        self.assertEqual(quota_windows(merged)[0]["remainingPercent"], 100)

    def test_missing_or_invalid_fields_do_not_fabricate_zero(self):
        payload = {
            "rateLimits": {
                "primary": {"windowDurationMins": 300},
                "secondary": {"usedPercent": True},
            },
            "rateLimitsByLimitId": {"unsafe id": {
                "primary": {"usedPercent": 10},
            }},
            "credential": "must-not-pass",
        }
        self.assertEqual(quota_windows(payload), [])
        encoded = json.dumps(sanitize_rate_limit_payload(payload))
        self.assertNotIn("credential", encoded)
        self.assertNotIn("must-not-pass", encoded)

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

    def test_login_timeout_clears_stale_state_and_allows_real_retry(self):
        client, factory = self.make_client()
        login = client.begin_login()
        with self.assertRaises(AppServerError) as caught:
            client.wait_for_login(login["loginId"], timeout=0.01)
        self.assertEqual(caught.exception.category, "login_timeout")
        self.assertIsNone(client._active_login)
        self.assertEqual(client._login_events, {})

        retry = client.begin_login()
        self.assertEqual(retry["loginId"], "login-1")
        starts = [
            message for message in factory.processes[0].received
            if message.get("method") == "account/login/start"
        ]
        self.assertEqual(len(starts), 2)

    def test_reset_login_clears_state_even_when_remote_cancel_fails(self):
        def handler(process, message):
            if message.get("method") == "account/login/cancel":
                process.emit({
                    "id": message["id"],
                    "error": {"code": -32000, "message": "SECRET remote detail"},
                })
            else:
                _base_handler(process, message)

        client, factory = self.make_client(handler)
        client.begin_login()
        client.reset_login()
        self.assertIsNone(client._active_login)
        self.assertEqual(client._login_events, {})
        client.begin_login()
        starts = [
            message for message in factory.processes[0].received
            if message.get("method") == "account/login/start"
        ]
        self.assertEqual(len(starts), 2)

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

    def test_thread_start_uses_official_read_only_sandbox_value(self):
        def handler(process, message):
            if message.get("method") == "thread/start":
                process.emit({"id": message["id"], "result": {
                    "thread": {"id": "thread-1"},
                    "instructionSources": [],
                }})
            else:
                _base_handler(process, message)

        client, factory = self.make_client(handler)
        thread_id = client.start_or_resume_conversation(
            thread_id=None,
            model="visible",
            base_instructions="base",
            developer_instructions="developer",
        )
        self.assertEqual(thread_id, "thread-1")
        request = next(
            item for item in factory.processes[0].received
            if item.get("method") == "thread/start"
        )
        self.assertEqual(request["params"]["sandbox"], "read-only")

    def test_thread_resume_uses_official_read_only_sandbox_value(self):
        def handler(process, message):
            if message.get("method") == "thread/resume":
                process.emit({"id": message["id"], "result": {
                    "thread": {"id": "thread-existing"},
                    "instructionSources": [],
                }})
            else:
                _base_handler(process, message)

        client, factory = self.make_client(handler)
        thread_id = client.start_or_resume_conversation(
            thread_id="thread-existing",
            model="visible",
            base_instructions="base",
            developer_instructions="developer",
        )
        self.assertEqual(thread_id, "thread-existing")
        request = next(
            item for item in factory.processes[0].received
            if item.get("method") == "thread/resume"
        )
        self.assertEqual(request["params"]["sandbox"], "read-only")

    def test_rejected_request_exposes_only_sanitized_diagnostics(self):
        def handler(process, message):
            if message.get("method") == "thread/start":
                process.emit({"id": message["id"], "error": {
                    "code": -32600,
                    "message": "secret prompt path token thread detail",
                }})
            else:
                _base_handler(process, message)

        client, _factory = self.make_client(handler)
        with patch("osia.i18n.get_language", return_value="en"):
            with self.assertRaises(AppServerError) as caught:
                client.start_or_resume_conversation(
                    thread_id=None,
                    model="visible",
                    base_instructions="base",
                    developer_instructions="developer",
                )
        error = caught.exception
        self.assertEqual(error.category, "request_rejected")
        self.assertEqual(error.method, "thread/start")
        self.assertEqual(error.server_code, -32600)
        self.assertIn("Please retry", str(error))
        self.assertNotIn("secret", str(error))

    def test_rejected_request_message_is_translated_and_actionable(self):
        from osia import i18n

        with patch("osia.i18n.get_language", return_value="fr"):
            message = i18n.t("chatgpt_account.request_rejected")
        self.assertTrue(message.startswith("ERROR:"))
        self.assertIn("Réessayez", message)
        self.assertIn("API", message)

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

    def test_command_and_file_items_are_refused_and_interrupted(self):
        for item_type in ("commandExecution", "fileChange"):
            with self.subTest(item_type=item_type):
                error, received, deltas = self.run_native_failure(item_type=item_type)
                self.assertTrue(error.native_action)
                self.assertTrue(error.controlled_refusal)
                self.assertFalse(error.allow_fallback)
                self.assertFalse(error.output_started)
                self.assertEqual(deltas, [])
                self.assertEqual(
                    str(error),
                    "OSIA couldn’t safely translate the action requested by ChatGPT. "
                    "No action was performed. An alternative workflow supported by "
                    "OSIA may be available.",
                )
                interrupt = next(
                    message for message in received
                    if message.get("method") == "turn/interrupt"
                )
                self.assertEqual(interrupt["params"], {
                    "threadId": "thread-native", "turnId": "turn-native",
                })
                self.assertFalse(any(
                    message.get("result", {}).get("decision") in {
                        "accept", "acceptForSession",
                    }
                    for message in received
                    if isinstance(message.get("result"), dict)
                ))

    def test_command_and_file_approvals_are_cancelled_without_fallback(self):
        for approval_method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            with self.subTest(approval_method=approval_method):
                error, received, _deltas = self.run_native_failure(
                    approval_method=approval_method,
                )
                approval = next(
                    message for message in received
                    if message.get("id") == 900 and "result" in message
                )
                self.assertEqual(approval["result"], {"decision": "cancel"})
                self.assertTrue(error.native_action)
                self.assertTrue(error.controlled_refusal)
                self.assertFalse(error.allow_fallback)
                self.assertEqual(
                    len([
                        message for message in received
                        if message.get("method") == "turn/interrupt"
                    ]),
                    1,
                )

    def test_partial_text_then_native_action_has_one_safe_terminal_failure(self):
        error, received, deltas = self.run_native_failure(
            item_type="commandExecution",
            delta="Partial account text.",
        )
        self.assertEqual(deltas, ["Partial account text."])
        self.assertTrue(error.output_started)
        self.assertTrue(error.native_action)
        self.assertTrue(error.controlled_refusal)
        self.assertFalse(error.allow_fallback)
        self.assertNotIn(str(error), deltas)
        self.assertEqual(
            len([
                message for message in received
                if message.get("method") == "turn/interrupt"
            ]),
            1,
        )

    def test_unknown_action_notification_fails_closed_with_safe_refusal(self):
        error, _received, _deltas = self.run_native_failure(
            notification_method="plugin/futureNativeAction",
        )
        self.assertEqual(error.category, "unknown_action_event")
        self.assertTrue(error.native_action)
        self.assertTrue(error.controlled_refusal)
        self.assertFalse(error.allow_fallback)
        self.assertIn("No action was performed", str(error))

    def test_native_action_refusal_uses_user_language_before_ui_locale(self):
        cases = (
            (
                "Continue the OSIA conversation below.\n\n"
                "[USER]\nHello.\n\n[ASSISTANT]\nHow can I help?\n\n"
                "[USER]\nCrée un fichier nommé essai sur mon bureau, puis "
                "ouvre-le pour moi.",
                "en",
                "OSIA n’a pas pu traduire en toute sécurité l’action demandée par "
                "ChatGPT. Aucune action n’a été effectuée. Une méthode alternative "
                "prise en charge par OSIA peut être disponible.",
            ),
            (
                "Please create a file named sample on my desktop, then open it "
                "for me.",
                "fr",
                "OSIA couldn’t safely translate the action requested by ChatGPT. "
                "No action was performed. An alternative workflow supported by OSIA "
                "may be available.",
            ),
        )
        for text, ui_language, message in cases:
            with self.subTest(text=text, ui_language=ui_language):
                error, _received, _deltas = self.run_native_failure(
                    item_type="fileChange", language=ui_language, text=text,
                )
                self.assertEqual(str(error), message)

    def test_neutral_native_action_refusal_falls_back_to_ui_locale(self):
        expected = {
            "en": "No action was performed.",
            "fr": "Aucune action n’a été effectuée.",
        }
        for language, fragment in expected.items():
            with self.subTest(language=language):
                error, _received, _deltas = self.run_native_failure(
                    item_type="fileChange", language=language, text="12345 !!!",
                )
                self.assertIn(fragment, str(error))

    def test_text_only_account_turn_remains_unchanged(self):
        expected = "Ordinary account response."

        def handler(process, message):
            if message.get("method") == "turn/start":
                process.emit({
                    "id": message["id"],
                    "result": {"turn": {"id": "turn-text"}},
                })
                process.emit({
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-text", "turnId": "turn-text",
                        "delta": expected,
                    },
                })
                process.emit({
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-text",
                        "turn": {"id": "turn-text", "status": "completed"},
                    },
                })
            else:
                _base_handler(process, message)

        client, factory = self.make_client(handler)
        deltas = []
        result = client.run_turn(
            thread_id="thread-text", text="test", model="visible",
            on_delta=deltas.append,
        )
        self.assertEqual(result.text, expected)
        self.assertEqual(deltas, [expected])
        self.assertFalse(any(
            message.get("method") == "turn/interrupt"
            for message in factory.processes[0].received
        ))

    def test_legacy_native_action_message_is_absent_from_user_facing_paths(self):
        root = Path(__file__).resolve().parents[1]
        legacy = (
            "ChatGPT attempted a native "
            "Codex action; OSIA cancelled the turn."
        )
        for relative_path in (
            "osia/chatgpt_account/app_server_client.py",
            "osia/chatgpt_account/provider.py",
            "osia/i18n.py",
            "osia/main.py",
            "osia/server/server.py",
        ):
            with self.subTest(path=relative_path):
                source = (root / relative_path).read_text(encoding="utf-8")
                self.assertNotIn(legacy, source)

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
        self.assertTrue(caught.exception.controlled_refusal)
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
                self.assertTrue(caught.exception.controlled_refusal)
                self.assertFalse(caught.exception.allow_fallback)


if __name__ == "__main__":
    unittest.main()
