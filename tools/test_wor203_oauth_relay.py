"""Focused WOR-203 PC tests; no provider, relay or Cloudflare network access."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from osia import oauth_relay
from osia.integrations import google_oauth
from osia.mcp_client import oauth as mcp_oauth
from osia.relay_connector import RelayBridge


PAIR_ID = "b" * 64
STATE = "a" * 43


def _envelope(message_type: str, payload: dict) -> dict:
    return {
        "v": 2,
        "type": message_type,
        "role": "pc",
        "pair_id": PAIR_ID,
        "seq": 0,
        "ack": 1,
        "payload": payload,
    }


class _Transport:
    def __init__(
        self,
        coordinator: oauth_relay.OAuthRelayCoordinator,
        *,
        confirm: bool = True,
        rejection: str = "",
    ) -> None:
        self.coordinator = coordinator
        self.confirm = confirm
        self.rejection = rejection
        self.sent: list[tuple[str, dict]] = []

    def send_oauth_control(self, message_type: str, payload: dict) -> None:
        self.sent.append((message_type, dict(payload)))
        if message_type != oauth_relay.TYPE_OAUTH_FLOW_REGISTER:
            return
        if self.rejection:
            self.coordinator.handle_control_error(self.rejection)
        elif self.confirm:
            self.coordinator.handle_flow_registered(
                _envelope(
                    oauth_relay.TYPE_OAUTH_FLOW_REGISTERED,
                    {
                        "schema_version": 1,
                        "callback_id": payload["callback_id"],
                        "expires_at": int(
                            (time.time() + payload["ttl_seconds"]) * 1000
                        ),
                    },
                ),
                PAIR_ID,
            )


def _ready_coordinator(*, confirm: bool = True, rejection: str = ""):
    coordinator = oauth_relay.OAuthRelayCoordinator()
    transport = _Transport(coordinator, confirm=confirm, rejection=rejection)
    coordinator.bind_transport(transport)
    coordinator.mark_authoritative(transport)
    return coordinator, transport


def _callback(
    callback_id: str,
    expires_at: int,
    *,
    flow_kind: str = "gmail",
    code: str | None = "code-value",
    error: str | None = None,
) -> dict:
    payload = {
        "schema_version": 1,
        "callback_id": callback_id,
        "flow_kind": flow_kind,
        "expires_at": expires_at,
    }
    if code is not None:
        payload["code"] = code
    else:
        payload["error"] = error or "access_denied"
    return _envelope(oauth_relay.TYPE_OAUTH_CALLBACK, payload)


class OAuthRelayCoordinatorTests(unittest.TestCase):
    def test_callback_id_is_exact_lowercase_sha256(self):
        expected = "66d34fba71f8f450f7e45598853e53bfc23bbd129027cbb131a2f4ffd7878cd0"
        self.assertEqual(oauth_relay.callback_id_for_state(STATE), expected)
        self.assertRegex(expected, r"^[0-9a-f]{64}$")

    def test_callback_before_confirmation_and_malformed_callback_fail_closed(self):
        coordinator, transport = _ready_coordinator(confirm=False)
        failures: list[Exception] = []

        def register() -> None:
            try:
                coordinator.register_flow(
                    state=STATE,
                    flow_kind="gmail",
                    owner="ordering",
                    claim_callback=lambda *_args: True,
                    timeout=0.1,
                )
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=register)
        thread.start()
        deadline = time.time() + 1.0
        while not transport.sent and time.time() < deadline:
            time.sleep(0.001)
        callback_id = oauth_relay.callback_id_for_state(STATE)
        flow = coordinator._flows[callback_id]
        expires_at = int(flow.expires_at * 1000)

        unsafe_expiry = _envelope(
            oauth_relay.TYPE_OAUTH_FLOW_REGISTERED,
            {
                "schema_version": 1,
                "callback_id": callback_id,
                "expires_at": 1 << 53,
            },
        )
        self.assertFalse(coordinator.handle_flow_registered(unsafe_expiry, PAIR_ID))

        early_ack, _, early_reason = coordinator.handle_callback(
            _callback(callback_id, expires_at), PAIR_ID
        )
        self.assertEqual(
            (early_ack["disposition"], early_reason),
            ("rejected", "callback_not_registered"),
        )

        malformed = _callback(callback_id, expires_at)
        malformed["payload"]["unexpected"] = True
        malformed_ack, _, malformed_reason = coordinator.handle_callback(
            malformed, PAIR_ID
        )
        self.assertEqual(
            (malformed_ack["disposition"], malformed_reason),
            ("rejected", "malformed_callback"),
        )

        thread.join(timeout=1.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(
            failures[0], oauth_relay.OAuthRelayRegistrationTimeout
        )

    def test_registration_requires_authoritative_authenticated_transport(self):
        coordinator = oauth_relay.OAuthRelayCoordinator()
        transport = _Transport(coordinator)
        coordinator.bind_transport(transport)
        with self.assertRaises(oauth_relay.OAuthRelayUnavailable):
            coordinator.register_flow(
                state=STATE,
                flow_kind="gmail",
                owner="attempt",
                claim_callback=lambda *_args: True,
            )
        self.assertEqual(transport.sent, [])

    def test_registration_schema_and_confirmation_gate(self):
        coordinator, transport = _ready_coordinator()
        registration = coordinator.register_flow(
            state=STATE,
            flow_kind="gmail",
            owner="attempt",
            claim_callback=lambda *_args: True,
        )
        self.assertEqual(registration.callback_id, oauth_relay.callback_id_for_state(STATE))
        self.assertEqual(
            transport.sent[0],
            (
                "OAUTH_FLOW_REGISTER",
                {
                    "schema_version": 1,
                    "callback_id": registration.callback_id,
                    "flow_kind": "gmail",
                    "ttl_seconds": 165,
                },
            ),
        )

    def test_registration_timeout_and_worker_rejection_fail_closed(self):
        coordinator, transport = _ready_coordinator(confirm=False)
        with self.assertRaises(oauth_relay.OAuthRelayRegistrationTimeout):
            coordinator.register_flow(
                state=STATE,
                flow_kind="mcp",
                owner="flow",
                claim_callback=lambda *_args: True,
                timeout=0.01,
            )
        self.assertEqual(transport.sent[-1][0], "OAUTH_FLOW_CANCEL")

        rejected, _transport = _ready_coordinator(rejection="oauth_flow_quota")
        with self.assertRaises(oauth_relay.OAuthRelayRegistrationRejected) as caught:
            rejected.register_flow(
                state="c" * 43,
                flow_kind="mcp",
                owner="flow",
                claim_callback=lambda *_args: True,
            )
        self.assertEqual(caught.exception.reason, "oauth_flow_quota")

    def test_claim_replay_wrong_kind_cancel_and_expiry(self):
        coordinator, transport = _ready_coordinator()
        claims: list[tuple] = []
        registration = coordinator.register_flow(
            state=STATE,
            flow_kind="gmail",
            owner="attempt",
            claim_callback=lambda *args: claims.append(args) or True,
        )
        expires_ms = int(registration.expires_at * 1000)
        ack, completion, reason = coordinator.handle_callback(
            _callback(registration.callback_id, expires_ms), PAIR_ID
        )
        self.assertEqual((ack["disposition"], completion, reason), ("consumed", None, "accepted"))
        self.assertEqual(claims, [(STATE, "code-value", None)])
        replay_ack, _completion, replay_reason = coordinator.handle_callback(
            _callback(registration.callback_id, expires_ms), PAIR_ID
        )
        self.assertEqual(replay_ack["disposition"], "consumed")
        self.assertEqual(replay_reason, "callback_replayed")
        self.assertEqual(len(claims), 1)

        wrong, _ = _ready_coordinator()
        wrong_registration = wrong.register_flow(
            state="d" * 43,
            flow_kind="gmail",
            owner="wrong",
            claim_callback=lambda *_args: True,
        )
        wrong_ack, _, wrong_reason = wrong.handle_callback(
            _callback(
                wrong_registration.callback_id,
                int(wrong_registration.expires_at * 1000),
                flow_kind="mcp",
            ),
            PAIR_ID,
        )
        self.assertEqual((wrong_ack["disposition"], wrong_reason), ("rejected", "wrong_flow_kind"))

        cancelled, cancel_transport = _ready_coordinator()
        cancelled_registration = cancelled.register_flow(
            state="e" * 43,
            flow_kind="mcp",
            owner="cancel",
            claim_callback=lambda *_args: True,
        )
        self.assertTrue(cancelled.cancel_flow(callback_id=cancelled_registration.callback_id))
        self.assertEqual(cancel_transport.sent[-1][0], "OAUTH_FLOW_CANCEL")

        expired, _ = _ready_coordinator()
        expired_registration = expired.register_flow(
            state="f" * 43,
            flow_kind="mcp",
            owner="expired",
            claim_callback=lambda *_args: True,
        )
        expired._flows[expired_registration.callback_id].expires_at = time.time() - 1
        expired_ack, _, expired_reason = expired.handle_callback(
            _callback(
                expired_registration.callback_id,
                int(expired_registration.expires_at * 1000),
                flow_kind="mcp",
            ),
            PAIR_ID,
        )
        self.assertEqual((expired_ack["disposition"], expired_reason), ("rejected", "callback_expired"))

    def test_reconnect_preserves_mapping_and_fresh_process_rejects_replay(self):
        coordinator, first = _ready_coordinator()
        registration = coordinator.register_flow(
            state=STATE,
            flow_kind="mcp",
            owner="flow",
            claim_callback=lambda *_args: True,
        )
        coordinator.mark_disconnected(first)
        second = _Transport(coordinator)
        coordinator.bind_transport(second)
        coordinator.mark_authoritative(second)
        ack, _, reason = coordinator.handle_callback(
            _callback(
                registration.callback_id,
                int(registration.expires_at * 1000),
                flow_kind="mcp",
            ),
            PAIR_ID,
        )
        self.assertEqual((ack["disposition"], reason), ("consumed", "accepted"))

        restarted = oauth_relay.OAuthRelayCoordinator()
        replay_ack, _, restart_reason = restarted.handle_callback(
            _callback(
                registration.callback_id,
                int(registration.expires_at * 1000),
                flow_kind="mcp",
            ),
            PAIR_ID,
        )
        self.assertEqual(replay_ack["disposition"], "rejected")
        self.assertEqual(restart_reason, "callback_after_restart")

    def test_relay_consumes_oauth_control_without_mobile_forwarding(self):
        async def scenario():
            oauth_relay.coordinator.reset_for_tests()
            transport = _Transport(oauth_relay.coordinator)
            oauth_relay.coordinator.bind_transport(transport)
            oauth_relay.coordinator.mark_authoritative(transport)
            registration = oauth_relay.coordinator.register_flow(
                state=STATE,
                flow_kind="gmail",
                owner="relay-test",
                claim_callback=lambda *_args: True,
            )

            class RelaySocket:
                def __init__(self):
                    self.sent: list[str] = []

                async def send(self, value: str) -> None:
                    self.sent.append(value)

            bridge = RelayBridge("test-token", "wss://relay.test/ws")
            bridge.pair_id = PAIR_ID
            forwarded: list[dict] = []

            async def forward(frame):
                forwarded.append(frame)
                return True

            bridge._forward_mobile_frame = forward
            relay_socket = RelaySocket()
            await bridge._handle_relay_frame(
                relay_socket,
                _callback(
                    registration.callback_id,
                    int(registration.expires_at * 1000),
                ),
            )
            self.assertEqual(forwarded, [])
            sent = json.loads(relay_socket.sent[0])
            self.assertEqual(sent["type"], "OAUTH_CALLBACK_ACK")
            self.assertEqual(sent["payload"]["disposition"], "consumed")

        asyncio.run(scenario())


class GmailRelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        oauth_relay.coordinator.reset_for_tests()
        self.transport = _Transport(oauth_relay.coordinator)
        oauth_relay.coordinator.bind_transport(self.transport)
        oauth_relay.coordinator.mark_authoritative(self.transport)
        with google_oauth._REMOTE_FLOW_LOCK:
            google_oauth._REMOTE_FLOWS.clear()
            google_oauth._REMOTE_FLOW_BY_INTEGRATION.clear()
            google_oauth._REMOTE_RESULTS.clear()
        if google_oauth._AUTH_FLOW_LOCK.locked():
            google_oauth._AUTH_FLOW_LOCK.release()

    def tearDown(self):
        google_oauth.cancel_remote_authorization("gmail-rest")
        oauth_relay.coordinator.reset_for_tests()

    @staticmethod
    def _client():
        return google_oauth.GoogleOAuthClient(
            "public-client",
            "private-client-secret",
            redirect_uris=(
                google_oauth.OAUTH_REDIRECT_URI,
                oauth_relay.OAUTH_RELAY_REDIRECT_URI,
            ),
        )

    def test_url_is_withheld_until_registered_and_pkce_stays_local(self):
        with patch.object(google_oauth, "load_client_config", lambda _key: self._client()):
            result = google_oauth.begin_remote_authorization(
                "gmail-rest",
                ["scope.read"],
                redirect_uri=oauth_relay.OAUTH_RELAY_REDIRECT_URI,
            )
        query = parse_qs(urlsplit(result["auth_url"]).query)
        self.assertEqual(query["redirect_uri"], [oauth_relay.OAUTH_RELAY_REDIRECT_URI])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("verifier", json.dumps(result))
        self.assertEqual(self.transport.sent[0][0], "OAUTH_FLOW_REGISTER")

    def test_exact_local_state_claims_before_local_token_exchange(self):
        exchanges: list[tuple[str, str]] = []
        stored: list[dict] = []

        def exchange(_client, code, _verifier, *, redirect_uri):
            exchanges.append((code, redirect_uri))
            return {"access_token": "new-token", "expires_in": 3600}

        with patch.object(google_oauth, "load_client_config", lambda _key: self._client()), patch.object(
            google_oauth, "_exchange_authorization_code", exchange
        ), patch.object(google_oauth, "get_oauth_token", lambda _key: {}), patch.object(
            google_oauth, "_store_token", lambda _key, token, _client: stored.append(dict(token))
        ):
            result = google_oauth.begin_remote_authorization(
                "gmail-rest", ["scope.read"], redirect_uri=oauth_relay.OAUTH_RELAY_REDIRECT_URI
            )
            state = next(iter(google_oauth._REMOTE_FLOWS))
            callback_id = oauth_relay.callback_id_for_state(state)
            ack, completion, reason = oauth_relay.coordinator.handle_callback(
                _callback(callback_id, int(result["expires_at"] * 1000)), PAIR_ID
            )
            self.assertEqual((ack["disposition"], reason), ("consumed", "accepted"))
            self.assertEqual(exchanges, [])
            completion()

        self.assertEqual(exchanges, [("code-value", oauth_relay.OAUTH_RELAY_REDIRECT_URI)])
        self.assertEqual(len(stored), 1)

    def test_provider_refusal_and_exchange_failure_are_safe_after_claim(self):
        with patch.object(google_oauth, "load_client_config", lambda _key: self._client()):
            result = google_oauth.begin_remote_authorization(
                "gmail-rest", ["scope.read"], redirect_uri=oauth_relay.OAUTH_RELAY_REDIRECT_URI
            )
        state = next(iter(google_oauth._REMOTE_FLOWS))
        ack, completion, _ = oauth_relay.coordinator.handle_callback(
            _callback(
                oauth_relay.callback_id_for_state(state),
                int(result["expires_at"] * 1000),
                code=None,
                error="access_denied",
            ),
            PAIR_ID,
        )
        self.assertEqual(ack["disposition"], "consumed")
        refused = completion()
        self.assertEqual(refused["error_category"], "authorization_denied")

        exchanges: list[str] = []
        stores: list[dict] = []

        def fail_exchange(_client, code, _verifier, *, redirect_uri):
            exchanges.append(code)
            raise google_oauth.GoogleOAuthError("synthetic private provider detail")

        with patch.object(google_oauth, "load_client_config", lambda _key: self._client()), patch.object(
            google_oauth, "_exchange_authorization_code", fail_exchange
        ), patch.object(
            google_oauth, "_store_token", lambda *_args: stores.append({})
        ):
            failed = google_oauth.begin_remote_authorization(
                "gmail-rest", ["scope.read"], redirect_uri=oauth_relay.OAUTH_RELAY_REDIRECT_URI
            )
            failed_state = next(iter(google_oauth._REMOTE_FLOWS))
            failed_callback_id = oauth_relay.callback_id_for_state(failed_state)
            failed_message = _callback(
                failed_callback_id, int(failed["expires_at"] * 1000)
            )
            failed_ack, failed_completion, _ = oauth_relay.coordinator.handle_callback(
                failed_message, PAIR_ID
            )
            self.assertEqual(failed_ack["disposition"], "consumed")
            failure = failed_completion()
            replay_ack, replay_completion, replay_reason = (
                oauth_relay.coordinator.handle_callback(failed_message, PAIR_ID)
            )

        self.assertEqual(failure["error_category"], "authentication_failed")
        self.assertEqual(exchanges, ["code-value"])
        self.assertEqual(stores, [])
        self.assertEqual(replay_ack["disposition"], "consumed")
        self.assertIsNone(replay_completion)
        self.assertEqual(replay_reason, "callback_replayed")

    def test_desktop_loopback_constant_is_unchanged(self):
        self.assertEqual(google_oauth.OAUTH_REDIRECT_URI, "http://127.0.0.1:8975/callback")


class McpRelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        oauth_relay.coordinator.reset_for_tests()
        self.transport = _Transport(oauth_relay.coordinator)
        oauth_relay.coordinator.bind_transport(self.transport)
        oauth_relay.coordinator.mark_authoritative(self.transport)
        with mcp_oauth._OAUTH_REG_LOCK:
            mcp_oauth._OAUTH_STATE_FLOWS.clear()
            mcp_oauth._OAUTH_TRIGGER_FLOWS.clear()

    def tearDown(self):
        oauth_relay.coordinator.reset_for_tests()

    def test_sdk_flow_is_registered_then_resolved_exactly_once(self):
        async def scenario():
            flow_id = "flow-owner"
            mcp_oauth.register_oauth_trigger_flow(flow_id)
            callback = mcp_oauth.TunnelOAuthCallback(flow_id)
            url = (
                "https://login.connector.example/authorize?state="
                + STATE
                + "&redirect_uri="
                + oauth_relay.OAUTH_RELAY_REDIRECT_URI
                + "&code_challenge=challenge&code_challenge_method=S256"
            )
            await callback.redirect_handler(url)
            surfaced, error = mcp_oauth.wait_oauth_auth_url(flow_id, 0.01)
            self.assertEqual((surfaced, error), (url, None))
            registration = self.transport.sent[0][1]
            expires_at = oauth_relay.coordinator._flows[
                registration["callback_id"]
            ].worker_expires_at_ms
            ack, completion, reason = oauth_relay.coordinator.handle_callback(
                _callback(
                    registration["callback_id"],
                    expires_at,
                    flow_kind="mcp",
                ),
                PAIR_ID,
            )
            self.assertEqual((ack["disposition"], completion, reason), ("consumed", None, "accepted"))
            self.assertEqual(await callback.callback_handler(), ("code-value", STATE))
            replay_ack, _, replay_reason = oauth_relay.coordinator.handle_callback(
                _callback(
                    registration["callback_id"],
                    expires_at,
                    flow_kind="mcp",
                ),
                PAIR_ID,
            )
            self.assertEqual((replay_ack["disposition"], replay_reason), ("consumed", "callback_replayed"))

        asyncio.run(scenario())

    def test_dcr_reuse_or_targeted_metadata_invalidation_preserves_tokens(self):
        stable_blob = {
            "token": {"access_token": "keep-token"},
            "client": {"client_id": "registered", "redirect_uris": [oauth_relay.OAUTH_RELAY_REDIRECT_URI]},
        }
        writes: list[dict] = []
        with patch.object(mcp_oauth, "get_mcp_oauth", lambda _sid: stable_blob), patch.object(
            mcp_oauth, "set_mcp_oauth", lambda _sid, blob: writes.append(dict(blob))
        ):
            self.assertEqual(
                mcp_oauth.prepare_dcr_registration_for_redirect("connector"),
                "registration_reused",
            )
        self.assertEqual(writes, [])

        old_blob = {
            "token": {"access_token": "keep-token", "refresh_token": "keep-refresh"},
            "client": {"client_id": "old", "redirect_uris": [mcp_oauth.OAUTH_REDIRECT_URI]},
            "other": "keep",
        }
        with patch.object(mcp_oauth, "get_mcp_oauth", lambda _sid: old_blob), patch.object(
            mcp_oauth, "set_mcp_oauth", lambda _sid, blob: writes.append(dict(blob))
        ):
            self.assertEqual(
                mcp_oauth.prepare_dcr_registration_for_redirect("connector"),
                "registration_invalidated",
            )
        self.assertNotIn("client", writes[-1])
        self.assertEqual(writes[-1]["token"], old_blob["token"])
        self.assertEqual(writes[-1]["other"], "keep")

    def test_desktop_loopback_transport_is_unchanged(self):
        self.assertEqual(mcp_oauth.OAUTH_REDIRECT_URI, "http://127.0.0.1:8975/callback")
        self.assertTrue(hasattr(mcp_oauth.OAuthCallbackServer, "callback_handler"))


class McpTokenAuthenticationCompatibilityTests(unittest.TestCase):
    class _Callback:
        async def redirect_handler(self, _url):
            return None

        async def callback_handler(self):
            return "synthetic-code", "synthetic-state"

    @staticmethod
    def _provider(auth_method: str, *, secret: str | None):
        from mcp.shared.auth import OAuthClientInformationFull

        provider = mcp_oauth.build_oauth_provider(
            "synthetic-connector",
            "https://connector.example/mcp",
            McpTokenAuthenticationCompatibilityTests._Callback(),
            redirect_uris=[oauth_relay.OAUTH_RELAY_REDIRECT_URI],
        )
        provider.context.client_info = OAuthClientInformationFull(
            client_id="synthetic-client",
            client_secret=secret,
            token_endpoint_auth_method=auth_method,
            redirect_uris=[oauth_relay.OAUTH_RELAY_REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )
        return provider

    def _send_once(self, request):
        import httpx

        seen = []

        def handler(sent):
            seen.append(sent)
            return httpx.Response(200, json={"ok": True})

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                response = await client.send(request)
                self.assertEqual(response.status_code, 200)

        asyncio.run(scenario())
        self.assertEqual(len(seen), 1)
        return seen[0]

    @staticmethod
    def _form(request):
        return parse_qs(request.content.decode("ascii"), keep_blank_values=True)

    def test_authorization_code_client_secret_basic_uses_header_only(self):
        provider = self._provider("client_secret_basic", secret="synthetic-secret")

        async def build_request():
            return await provider._exchange_token_authorization_code(
                "synthetic-code", "synthetic-verifier"
            )

        sent = self._send_once(asyncio.run(build_request()))
        form = self._form(sent)
        self.assertTrue(sent.headers["Authorization"].startswith("Basic "))
        encoded = sent.headers["Authorization"].removeprefix("Basic ")
        self.assertEqual(
            base64.b64decode(encoded).decode("utf-8"),
            "synthetic-client:synthetic-secret",
        )
        self.assertNotIn("client_id", form)
        self.assertNotIn("client_secret", form)
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["synthetic-code"])
        self.assertEqual(
            form["redirect_uri"], [oauth_relay.OAUTH_RELAY_REDIRECT_URI]
        )
        self.assertEqual(form["code_verifier"], ["synthetic-verifier"])

    def test_refresh_client_secret_basic_uses_header_only(self):
        from mcp.shared.auth import OAuthToken

        provider = self._provider("client_secret_basic", secret="synthetic-secret")
        provider.context.current_tokens = OAuthToken(
            access_token="synthetic-access",
            refresh_token="synthetic-refresh",
        )

        sent = self._send_once(asyncio.run(provider._refresh_token()))
        form = self._form(sent)
        self.assertTrue(sent.headers["Authorization"].startswith("Basic "))
        self.assertNotIn("client_id", form)
        self.assertNotIn("client_secret", form)
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["synthetic-refresh"])

    def test_post_and_public_auth_are_unchanged_and_credentials_are_not_logged(self):
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            post = self._provider("client_secret_post", secret="synthetic-secret")
            post_request = asyncio.run(
                post._exchange_token_authorization_code(
                    "synthetic-code", "synthetic-verifier"
                )
            )
            post_sent = self._send_once(post_request)
            post_form = self._form(post_sent)
            self.assertNotIn("Authorization", post_sent.headers)
            self.assertEqual(post_form["client_id"], ["synthetic-client"])
            self.assertEqual(post_form["client_secret"], ["synthetic-secret"])

            public = self._provider("none", secret=None)
            public_request = asyncio.run(
                public._exchange_token_authorization_code(
                    "synthetic-public-code", "synthetic-public-verifier"
                )
            )
            public_sent = self._send_once(public_request)
            public_form = self._form(public_sent)
            self.assertNotIn("Authorization", public_sent.headers)
            self.assertEqual(public_form["client_id"], ["synthetic-client"])
            self.assertNotIn("client_secret", public_form)
        finally:
            root.removeHandler(handler)

        log_text = "\n".join(captured)
        for private_value in (
            "synthetic-secret",
            "synthetic-code",
            "synthetic-verifier",
            "synthetic-public-code",
            "synthetic-public-verifier",
            "synthetic-state",
        ):
            self.assertNotIn(private_value, log_text)


if __name__ == "__main__":
    unittest.main()
