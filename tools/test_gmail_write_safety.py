#!/usr/bin/env python3
"""Focused WOR-101/102 Gmail write-safety and OAuth-flow smokes."""

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
from osia import oauth_relay
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


class _ConfirmingOAuthRelay:
    def send_oauth_control(self, message_type: str, payload: dict) -> None:
        if message_type != oauth_relay.TYPE_OAUTH_FLOW_REGISTER:
            return
        oauth_relay.coordinator.handle_flow_registered(
            {
                "v": 2,
                "type": oauth_relay.TYPE_OAUTH_FLOW_REGISTERED,
                "role": "pc",
                "pair_id": "b" * 64,
                "seq": 0,
                "ack": 1,
                "payload": {
                    "schema_version": 1,
                    "callback_id": payload["callback_id"],
                    "expires_at": int(
                        (google_oauth.time.time() + payload["ttl_seconds"]) * 1000
                    ),
                },
            },
            "b" * 64,
        )


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

    def test_interactive_gmail_write_requires_confirmation(self):
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

    def test_legacy_auto_approve_value_cannot_skip_confirmation(self):
        prompts: list[tuple[str, str, str]] = []
        api_calls: list = []

        def provider(server_id, tool_name, summary):
            prompts.append((server_id, tool_name, summary))
            return True

        registry.set_confirmation_provider(provider)
        with write_settings(confirm_writes=False, unattended=False), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertEqual(len(prompts), 1)
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

    def test_legacy_unattended_on_without_provider_fails_closed(self):
        api_calls: list = []

        with write_settings(confirm_writes=True, unattended=True), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(api_calls, [])

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

    def test_legacy_unattended_on_without_target_fails_closed(self):
        api_calls: list = []
        registry.set_confirmation_provider(lambda *_args: None)

        with write_settings(confirm_writes=True, unattended=True), patched(
            gmail, "_require_compose_permission", lambda: None
        ), self._patch_draft_create(api_calls):
            result = gmail.gmail_create_draft(
                "user@example.com", "Subject", "Body"
            )

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("ERROR:"))
        self.assertEqual(api_calls, [])

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
        registry.set_confirmation_provider(lambda *_args: True)

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

    def test_native_gmail_chat_tools_remain_in_pc_tool_map(self):
        from osia import agent

        for name in (
            "gmail_list_latest",
            "gmail_search",
            "gmail_read_message",
            "gmail_read_thread",
            "gmail_create_draft",
            "gmail_send_draft",
            "gmail_mark_read",
            "gmail_mark_unread",
            "gmail_apply_label",
            "gmail_remove_label",
        ):
            self.assertIn(name, agent.TOOL_MAP)


class MobileGmailSnapshotTests(unittest.TestCase):
    def test_missing_mobile_oauth_configuration_is_safe_and_precise(self):
        from osia.server import server as server_mod

        disconnected = {
            "connected": False,
            "status": "disconnected",
            "emailAddress": None,
            "capabilities": {},
            "access_token": "must-not-leak",
            "refresh_token": "must-not-leak-either",
            "client_secret": "also-must-not-leak",
        }
        with patched(
            gmail, "gmail_connection_status", lambda validate=False: disconnected
        ), patched(
            google_oauth, "mobile_redirect_uri", lambda: ""
        ), patched(
            google_oauth, "oauth_client_configured", lambda _key: False
        ), patched(
            google_oauth,
            "oauth_client_supports_redirect",
            lambda _key, _uri: False,
        ), patched(
            google_oauth,
            "remote_authorization_status",
            lambda _key: {
                "connecting": False,
                "status": "authorization_required",
                "error_category": "",
                "message": "",
            },
        ):
            snapshot = server_mod._gmail_rest_config_snapshot()

        self.assertEqual(snapshot["status"], "configuration_missing")
        self.assertEqual(snapshot["errorCategory"], "configuration_missing")
        self.assertFalse(snapshot["oauthClientConfigured"])
        self.assertFalse(snapshot["mobileOAuthConfigured"])
        self.assertTrue(snapshot["configureOnPc"])
        self.assertFalse(snapshot["canConnect"])
        payload = json.dumps(snapshot, sort_keys=True)
        for forbidden in (
            "must-not-leak",
            "must-not-leak-either",
            "also-must-not-leak",
            "access_token",
            "refresh_token",
            "client_secret",
            "C:\\\\Users\\\\",
            "/home/",
        ):
            self.assertNotIn(forbidden, payload)

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
            google_oauth,
            "mobile_redirect_uri",
            lambda: "https://callback.example/oauth/cb/" + ("a" * 64),
        ), patched(
            google_oauth, "oauth_client_configured", lambda _key: True
        ), patched(
            google_oauth,
            "oauth_client_supports_redirect",
            lambda _key, _uri: True,
        ), patched(
            google_oauth,
            "remote_authorization_status",
            lambda _key: {
                "connecting": False,
                "status": "authorization_required",
                "error_category": "",
                "message": "",
            },
        ), patched(
            personality,
            "resolve_personality",
            lambda: personality.PersonalitySelection(
                "default", "", "test", "missing", ""
            ),
        ), patched(
            personality, "discover_skills", lambda: []
        ):
            cfg = server_mod._build_config_snapshot()

        self.assertNotIn("island_x", cfg)
        self.assertNotIn("island_y", cfg)
        self.assertEqual(
            cfg["gmail_rest"],
            {
                "type": "native_rest",
                "id": "gmail-rest",
                "connected": True,
                "connecting": False,
                "status": "connected",
                "emailAddress": "me@example.com",
                "capabilities": {
                    "readonly": True,
                    "compose": True,
                    "modify": False,
                },
                "errorCategory": "",
                "message": "",
                "reason": "",
                "oauthClientConfigured": True,
                "mobileOAuthConfigured": True,
                "userActionRequired": False,
                "attemptId": None,
                "attemptExpiresAt": None,
                "canConnect": False,
                "canCancel": False,
                "canReconnect": True,
                "canDisconnect": True,
                "configureOnPc": False,
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
                "mcp_connect", "gmail"
            )
        )
        self.assertTrue(
            server_mod._is_native_gmail_rest_mobile_action(
                "mcp_connect", "gmail-rest"
            )
        )
        self.assertTrue(
            server_mod._is_native_gmail_rest_mobile_action(
                "mcp_oauth_cancel", "gmail-rest"
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
            google_oauth, "cancel_remote_authorization", lambda _key: False
        ), patched(
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


class MobileGoogleOAuthAttemptTests(unittest.TestCase):
    redirect_uri = oauth_relay.OAUTH_RELAY_REDIRECT_URI

    def setUp(self) -> None:
        oauth_relay.coordinator.reset_for_tests()
        transport = _ConfirmingOAuthRelay()
        oauth_relay.coordinator.bind_transport(transport)
        oauth_relay.coordinator.mark_authoritative(transport)

    def tearDown(self) -> None:
        for key in list(google_oauth._REMOTE_FLOW_BY_INTEGRATION):
            google_oauth.cancel_remote_authorization(key)
        with google_oauth._REMOTE_FLOW_LOCK:
            google_oauth._REMOTE_RESULTS.clear()
        oauth_relay.coordinator.reset_for_tests()

    def _client(self):
        return google_oauth.GoogleOAuthClient(
            "client-id",
            "client-secret",
            redirect_uris=(google_oauth.OAUTH_REDIRECT_URI, self.redirect_uri),
        )

    def _active_state(self) -> str:
        with google_oauth._REMOTE_FLOW_LOCK:
            return next(iter(google_oauth._REMOTE_FLOWS))

    def test_connect_is_bounded_and_idempotent(self):
        with patched(google_oauth, "load_client_config", lambda _key: self._client()):
            first = google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )
            second = google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["auth_url"], second["auth_url"])
        self.assertGreater(first["expires_at"], 0)
        self.assertLessEqual(
            first["expires_at"] - google_oauth.time.time(),
            google_oauth.REMOTE_AUTHORIZATION_TIMEOUT_S,
        )
        with google_oauth._REMOTE_FLOW_LOCK:
            self.assertEqual(len(google_oauth._REMOTE_FLOWS), 1)

    def test_cancel_and_timeout_return_to_retryable_state(self):
        with patched(google_oauth, "load_client_config", lambda _key: self._client()):
            first = google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )
            self.assertTrue(
                google_oauth.cancel_remote_authorization(
                    gmail.GMAIL_REST_KEY,
                    attempt_id=first["attempt_id"],
                )
            )
            self.assertFalse(
                google_oauth.remote_authorization_status(
                    gmail.GMAIL_REST_KEY
                )["connecting"]
            )
            second = google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )
            future = second["expires_at"] + 1.0
            with patched(google_oauth.time, "time", lambda: future):
                expired = google_oauth.remote_authorization_status(
                    gmail.GMAIL_REST_KEY
                )

        self.assertFalse(expired["connecting"])
        self.assertEqual(expired["status"], "authorization_timeout")
        self.assertEqual(expired["error_category"], "authorization_timeout")

    def test_successful_callback_stores_once_and_preserves_refresh_fields(self):
        stored: list[dict] = []
        exchanges: list[tuple[str, str]] = []

        def fake_exchange(_client, _code, _verifier, *, redirect_uri):
            exchanges.append((_code, redirect_uri))
            return {"access_token": "new-access", "expires_in": 3600}

        def fake_store(_key, token, _client):
            stored.append(dict(token))
            return {"token": dict(token)}

        old_blob = {
            "token": {
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "scope": gmail.GMAIL_READONLY_SCOPE,
            }
        }
        with patched(google_oauth, "load_client_config", lambda _key: self._client()), patched(
            google_oauth, "_exchange_authorization_code", fake_exchange
        ), patched(
            google_oauth, "get_oauth_token", lambda _key: old_blob
        ), patched(
            google_oauth, "_store_token", fake_store
        ):
            google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )
            state = self._active_state()
            result = google_oauth.complete_remote_authorization(
                state, "authorization-code", None
            )
            replay = google_oauth.complete_remote_authorization(
                state, "authorization-code", None
            )

        self.assertTrue(result["matched"])
        self.assertTrue(result["success"])
        self.assertFalse(replay["matched"])
        self.assertEqual(exchanges, [("authorization-code", self.redirect_uri)])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["access_token"], "new-access")
        self.assertEqual(stored[0]["refresh_token"], "old-refresh")
        self.assertEqual(stored[0]["scope"], gmail.GMAIL_READONLY_SCOPE)

    def test_failed_callback_preserves_old_token_and_classifies_safely(self):
        store_calls: list[bool] = []

        def fail_exchange(*_args, **_kwargs):
            raise google_oauth.GoogleOAuthError(
                "HTTP 401 invalid_grant raw-provider-body private-value"
            )

        with patched(google_oauth, "load_client_config", lambda _key: self._client()), patched(
            google_oauth, "_exchange_authorization_code", fail_exchange
        ), patched(
            google_oauth, "_store_token", lambda *_args: store_calls.append(True)
        ):
            google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )
            result = google_oauth.complete_remote_authorization(
                self._active_state(), "bad-code", None
            )

        self.assertTrue(result["matched"])
        self.assertFalse(result["success"])
        self.assertEqual(result["error_category"], "authentication_failed")
        self.assertEqual(store_calls, [])
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("private-value", serialized)
        self.assertNotIn("invalid_grant", serialized)
        self.assertNotIn("bad-code", serialized)

    def test_cancel_during_exchange_prevents_late_callback_token_install(self):
        exchange_started = threading.Event()
        release_exchange = threading.Event()
        store_calls: list[bool] = []

        def delayed_exchange(*_args, **_kwargs):
            exchange_started.set()
            self.assertTrue(release_exchange.wait(timeout=2.0))
            return {"access_token": "late-access", "expires_in": 3600}

        with patched(google_oauth, "load_client_config", lambda _key: self._client()), patched(
            google_oauth, "_exchange_authorization_code", delayed_exchange
        ), patched(
            google_oauth, "_store_token", lambda *_args: store_calls.append(True)
        ):
            attempt = google_oauth.begin_remote_authorization(
                gmail.GMAIL_REST_KEY,
                [gmail.GMAIL_READONLY_SCOPE],
                redirect_uri=self.redirect_uri,
            )
            state = self._active_state()
            result_holder: list[dict] = []
            worker = threading.Thread(
                target=lambda: result_holder.append(
                    google_oauth.complete_remote_authorization(
                        state, "late-code", None
                    )
                )
            )
            worker.start()
            self.assertTrue(exchange_started.wait(timeout=2.0))
            self.assertTrue(
                google_oauth.cancel_remote_authorization(
                    gmail.GMAIL_REST_KEY,
                    attempt_id=attempt["attempt_id"],
                )
            )
            release_exchange.set()
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(store_calls, [])
        self.assertEqual(result_holder[0]["error_category"], "authorization_cancelled")

    def test_oauth_url_frame_contains_no_credentials_or_private_path(self):
        from osia.server import server as server_mod

        result = {
            "auth_url": (
                "https://accounts.google.com/o/oauth2/v2/auth?"
                "client_id=public-id&state=opaque&code_challenge=challenge"
            ),
            "attempt_id": "attempt-safe",
            "expires_at": 12345.0,
        }
        frame = server_mod._gmail_rest_oauth_url_frame(result, "request-safe")
        self.assertEqual(frame["id"], "gmail-rest")
        self.assertEqual(frame["attempt_id"], "attempt-safe")
        payload = json.dumps(frame, sort_keys=True)
        for forbidden in (
            "access_token",
            "refresh_token",
            "client_secret",
            "authorization_code",
            "oauth_tokens.json",
            "C:\\\\Users\\\\",
            "/home/",
        ):
            self.assertNotIn(forbidden, payload)

    def test_second_live_mobile_cannot_take_over_pending_attempt(self):
        from osia.server import server as server_mod

        owner_one = object()
        owner_two = object()

        async def no_broadcast():
            return None

        async def scenario():
            server_mod._WS_CLIENTS.update({owner_one, owner_two})
            try:
                first = await server_mod._begin_native_gmail_rest_mobile_connect(
                    owner=owner_one
                )
                second = await server_mod._begin_native_gmail_rest_mobile_connect(
                    owner=owner_two
                )
                return first, second
            finally:
                server_mod._WS_CLIENTS.discard(owner_one)
                server_mod._WS_CLIENTS.discard(owner_two)

        with patched(google_oauth, "mobile_redirect_uri", lambda: self.redirect_uri), patched(
            google_oauth,
            "oauth_client_supports_redirect",
            lambda _key, _uri: True,
        ), patched(
            google_oauth, "load_client_config", lambda _key: self._client()
        ), patched(
            gmail, "gmail_reconnect_scopes", lambda: (gmail.GMAIL_READONLY_SCOPE,)
        ), patched(
            server_mod, "_broadcast_gmail_rest_config", no_broadcast
        ):
            first, second = asyncio.run(scenario())

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error_category"], "authorization_pending")

    def test_reconnect_scope_selection_preserves_capabilities(self):
        modify_blob = {"token": {"scope": " ".join(gmail.GMAIL_MODIFY_SCOPES)}}
        compose_blob = {"token": {"scope": " ".join(gmail.GMAIL_COMPOSE_SCOPES)}}
        with patched(gmail, "get_oauth_token", lambda _key: modify_blob):
            self.assertEqual(gmail.gmail_reconnect_scopes(), gmail.GMAIL_MODIFY_SCOPES)
        with patched(gmail, "get_oauth_token", lambda _key: compose_blob):
            self.assertEqual(gmail.gmail_reconnect_scopes(), gmail.GMAIL_COMPOSE_SCOPES)
        with patched(gmail, "get_oauth_token", lambda _key: {}):
            self.assertEqual(
                gmail.gmail_reconnect_scopes(), (gmail.GMAIL_READONLY_SCOPE,)
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
