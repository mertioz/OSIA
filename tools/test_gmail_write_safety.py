#!/usr/bin/env python3
"""Focused WOR-101 Gmail write-safety and OAuth-flow smokes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import osia.config as config
from osia.integrations import google_oauth
from osia.mcp_client import registry
from osia.tools import gmail


@contextmanager
def patched(obj, name: str, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


@contextmanager
def write_settings(*, confirm_writes: bool, unattended: bool):
    values = {
        "mcp_confirm_writes": confirm_writes,
        "mcp_allow_unattended_writes": unattended,
    }

    def fake_get_setting(key, default=None):
        return values.get(key, default)

    with patched(config, "get_setting", fake_get_setting):
        yield


class GmailWriteSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        registry.set_confirmation_provider(None)

    def tearDown(self) -> None:
        registry.set_confirmation_provider(None)

    def _patch_draft_create(self, api_calls: list):
        def fake_post(path, *, json_body, scopes, interactive=True):
            api_calls.append((path, json_body, scopes, interactive))
            return {
                "id": "draft-1",
                "message": {"id": "msg-1", "threadId": "thread-1"},
            }

        return patched(gmail, "_gmail_post", fake_post)

    def test_auto_approve_off_connected_ui_requires_confirmation(self):
        prompts: list[tuple[str, str, str]] = []
        api_calls: list = []

        def provider(server_id, tool_name, summary):
            prompts.append((server_id, tool_name, summary))
            return True

        registry.set_confirmation_provider(provider)
        with write_settings(confirm_writes=True, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][0:2], ("gmail", "gmail_create_draft"))
        self.assertEqual(len(api_calls), 1)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["changed"])

    def test_auto_approve_on_connected_ui_skips_confirmation(self):
        api_calls: list = []

        def provider(*_args):
            raise AssertionError("provider should not be called")

        registry.set_confirmation_provider(provider)
        with write_settings(confirm_writes=False, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(len(api_calls), 1)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["changed"])

    def test_unattended_off_without_provider_denies_without_api_call(self):
        api_calls: list = []

        with write_settings(confirm_writes=True, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(api_calls, [])

    def test_unattended_on_without_provider_allows_intended_execution(self):
        api_calls: list = []

        with write_settings(confirm_writes=True, unattended=True), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(len(api_calls), 1)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["changed"])

    def test_unattended_off_without_target_denies_without_api_call(self):
        api_calls: list = []
        registry.set_confirmation_provider(lambda *_args: None)

        with write_settings(confirm_writes=True, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(api_calls, [])

    def test_unattended_on_without_target_allows_intended_execution(self):
        api_calls: list = []
        registry.set_confirmation_provider(lambda *_args: None)

        with write_settings(confirm_writes=True, unattended=True), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(len(api_calls), 1)
        self.assertIsInstance(result, dict)
        self.assertTrue(result["changed"])

    def test_user_denial_overrides_unattended_on_without_api_call(self):
        api_calls: list = []
        registry.set_confirmation_provider(lambda *_args: False)

        with write_settings(confirm_writes=True, unattended=True), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(api_calls, [])
        self.assertIsInstance(result, dict)
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "user_declined_confirmation")

    def test_denial_makes_zero_api_calls(self):
        api_calls: list = []
        registry.set_confirmation_provider(lambda *_args: False)

        with write_settings(confirm_writes=True, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(api_calls, [])
        self.assertIsInstance(result, dict)
        self.assertFalse(result["changed"])
        self.assertEqual(result["reason"], "user_declined_confirmation")

    def test_send_denial_makes_zero_api_calls(self):
        fetch_calls: list = []
        send_calls: list = []
        registry.set_confirmation_provider(lambda *_args: False)

        def fake_fetch(*_args, **_kwargs):
            fetch_calls.append(True)
            return {}

        def fake_send(*_args, **_kwargs):
            send_calls.append(True)
            return {}

        with write_settings(confirm_writes=True, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), patched(gmail, "_fetch_draft_summary", fake_fetch), patched(
            gmail, "_gmail_post_no_retry", fake_send
        ):
            result = gmail.gmail_send_draft("draft_1")

        self.assertEqual(fetch_calls, [])
        self.assertEqual(send_calls, [])
        self.assertIsInstance(result, dict)
        self.assertFalse(result["changed"])

    def test_send_uses_single_no_retry_call(self):
        send_calls: list = []

        def fake_fetch(*_args, **_kwargs):
            return {"draftId": "draft_1", "to": "user@example.com"}

        def fake_send(*_args, **_kwargs):
            send_calls.append(True)
            raise gmail.GmailRestError("forced send failure")

        with write_settings(confirm_writes=False, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), patched(gmail, "_fetch_draft_summary", fake_fetch), patched(
            gmail, "_gmail_post_no_retry", fake_send
        ):
            result = gmail.gmail_send_draft("draft_1")

        self.assertEqual(len(send_calls), 1)
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("ERROR:"))


class MobileGmailSnapshotTests(unittest.TestCase):
    def test_snapshot_adds_safe_gmail_rest_and_sanitizes_legacy_mcp(self):
        from osia import personality
        from osia.server import server as server_mod

        servers = [
            {
                "id": "linear",
                "name": "Linear",
                "transport": "streamable_http",
                "url": "https://mcp.linear.app/mcp",
                "auth_type": "oauth",
                "enabled": True,
            },
            {
                "id": "github",
                "name": "GitHub",
                "transport": "streamable_http",
                "url": "https://api.githubcopilot.com/mcp/",
                "auth_type": "token",
                "enabled": True,
            },
            {
                "id": "gmail",
                "name": "Gmail",
                "transport": "streamable_http",
                "url": "https://gmailmcp.googleapis.com/mcp/v1",
                "auth_type": "oauth",
                "enabled": True,
                "headers": {"Authorization": "Bearer should-not-leak"},
                "client_secret": "gmail-client-secret",
                "refresh_token": "gmail-refresh-token",
            },
        ]

        def fake_load_settings():
            return {
                "mcp_servers": [dict(s) for s in servers],
                "island_x": 100,
                "island_y": 200,
                "mcp_confirm_writes": True,
                "mcp_allow_unattended_writes": False,
            }

        def fake_get_setting(key, default=None):
            if key == "api_key_fallbacks":
                return {}
            if key == "mcp_servers":
                return [dict(s) for s in servers]
            return default

        def fake_gmail_status(*, validate=True):
            self.assertFalse(validate)
            return {
                "connected": True,
                "status": "connected",
                "emailAddress": "me@example.com",
                "capabilities": {
                    "readOnly": True,
                    "compose": True,
                    "modify": False,
                },
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
                "access_token": "gmail-access-token",
            }

        with patched(config, "load_settings", fake_load_settings), patched(
            config, "load_api_keys", lambda: {}
        ), patched(config, "get_setting", fake_get_setting), patched(
            config, "get_mcp_token", lambda _sid: ""
        ), patched(gmail, "gmail_connection_status", fake_gmail_status), patched(
            personality, "load_personality", lambda: ""
        ), patched(
            personality, "list_skills", lambda: []
        ):
            cfg = server_mod._build_config_snapshot()

        self.assertNotIn("island_x", cfg)
        self.assertNotIn("island_y", cfg)
        self.assertEqual(
            cfg["gmail_rest"],
            {
                "connected": True,
                "status": "connected",
                "emailAddress": "me@example.com",
                "capabilities": {
                    "readonly": True,
                    "compose": True,
                    "modify": False,
                },
                "reason": "",
                "canDisconnect": True,
                "configureOnPc": True,
                "elevateOnPc": True,
                "guideAvailable": True,
            },
        )
        self.assertNotIn("scope", cfg["gmail_rest"])
        self.assertNotIn("access_token", cfg["gmail_rest"])
        for secret_key in (
            "access_token",
            "refresh_token",
            "client_secret",
            "clientSecret",
            "oauth_token",
            "token",
            "scope",
        ):
            self.assertNotIn(secret_key, cfg["gmail_rest"])

        by_id = {s["id"]: s for s in cfg["mcp_servers"]}
        self.assertIn("linear", by_id)
        self.assertIn("github", by_id)
        self.assertEqual(by_id["linear"]["url"], "https://mcp.linear.app/mcp")
        self.assertEqual(by_id["github"]["auth_type"], "token")
        self.assertNotIn("gmail", by_id)

        legacy = by_id["gmail-mcp-legacy"]
        self.assertFalse(legacy["enabled"])
        self.assertTrue(legacy["hidden"])
        self.assertTrue(legacy["temporarily_unavailable"])
        self.assertNotIn("headers", legacy)
        self.assertNotIn("client_secret", legacy)
        self.assertNotIn("refresh_token", legacy)

        safe_payload = json.dumps(
            {"gmail_rest": cfg["gmail_rest"], "mcp_servers": cfg["mcp_servers"]},
            sort_keys=True,
        )
        for needle in (
            "gmail-access-token",
            "gmail-client-secret",
            "gmail-refresh-token",
            "should-not-leak",
        ):
            self.assertNotIn(needle, safe_payload)

    def test_native_gmail_disconnect_action_broadcasts_safe_config(self):
        from osia.server import server as server_mod

        disconnect_calls: list[bool] = []
        broadcasts: list[dict] = []

        def fake_disconnect():
            disconnect_calls.append(True)
            return {"status": "disconnected"}

        async def fake_broadcast_config(frame, exclude=None):
            broadcasts.append(frame)

        def fake_run_coroutine_threadsafe(coro, loop):
            asyncio.run(coro)
            return object()

        self.assertTrue(
            server_mod._is_native_gmail_rest_mobile_action(
                "mcp_disconnect", "gmail-rest"
            )
        )
        self.assertFalse(
            server_mod._is_native_gmail_rest_mobile_action(
                "mcp_disconnect", "gmail"
            )
        )
        self.assertFalse(
            server_mod._is_native_gmail_rest_mobile_action(
                "mcp_connect", "gmail-rest"
            )
        )

        safe_cfg = {
            "gmail_rest": {
                "connected": False,
                "status": "disconnected",
                "canDisconnect": False,
                "configureOnPc": True,
                "elevateOnPc": False,
                "guideAvailable": True,
            },
            "mcp_servers": [
                {"id": "linear", "enabled": True},
                {"id": "github", "enabled": True},
            ],
        }
        with patched(gmail, "gmail_disconnect", fake_disconnect), patched(
            server_mod, "_build_config_snapshot", lambda: safe_cfg
        ), patched(
            server_mod, "_broadcast_config", fake_broadcast_config
        ), patched(
            server_mod.asyncio,
            "run_coroutine_threadsafe",
            fake_run_coroutine_threadsafe,
        ):
            server_mod._disconnect_native_gmail_rest_and_broadcast(object())

        self.assertEqual(disconnect_calls, [True])
        self.assertEqual(
            broadcasts,
            [{"type": "config", "config": safe_cfg}],
        )


class GoogleOAuthFlowTests(unittest.TestCase):
    def test_callback_timeout_budget_is_not_premature(self):
        self.assertGreaterEqual(google_oauth.CALLBACK_TIMEOUT_S, 45.0)

    def test_authorize_rejects_duplicate_pending_flow(self):
        entered = threading.Event()
        release = threading.Event()
        capture_calls: list = []
        errors: list[Exception] = []

        def fake_load_client(_key):
            return google_oauth.GoogleOAuthClient("client-id", "secret")

        def fake_capture(_auth_url, _state):
            capture_calls.append(True)
            entered.set()
            release.wait(timeout=5.0)
            return "code"

        def fake_exchange(_client, _code, _verifier):
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "scope": gmail.GMAIL_READONLY_SCOPE,
                "expires_in": 3600,
            }

        def fake_store(_key, token, _client):
            return {"token": dict(token), "expires_at": 123.0}

        def run_first():
            try:
                google_oauth.authorize(
                    gmail.GMAIL_REST_KEY, [gmail.GMAIL_READONLY_SCOPE]
                )
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with patched(google_oauth, "load_client_config", fake_load_client), patched(
            google_oauth, "_capture_authorization_code", fake_capture
        ), patched(
            google_oauth, "_exchange_authorization_code", fake_exchange
        ), patched(
            google_oauth, "_store_token", fake_store
        ):
            thread = threading.Thread(target=run_first)
            thread.start()
            self.assertTrue(entered.wait(timeout=2.0))
            with self.assertRaises(google_oauth.GoogleOAuthError):
                google_oauth.authorize(
                    gmail.GMAIL_REST_KEY, [gmail.GMAIL_READONLY_SCOPE]
                )
            release.set()
            thread.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(capture_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
