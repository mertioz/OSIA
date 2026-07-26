import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from osia import config
from osia import mobile_model_catalogue as catalogue
from osia.chatgpt_account.provider import (
    ChatGPTAccountProvider,
    add_account_change_listener,
)


def _account(*, connected=False, models=None):
    models = list(models or [])
    return {
        "providerId": "chatgpt_account",
        "runtimeAvailable": True,
        "connected": connected,
        "connecting": False,
        "loginCancellable": False,
        "available": bool(connected and models),
        "error": "" if connected else "disconnected",
        "email": "safe@example.test" if connected else "",
        "plan": "plus" if connected else "",
        "quotaWindows": (
            [{"label": "5 h", "remainingPercent": 88}] if connected else []
        ),
        "selectedModel": models[0]["id"] if models else "",
        "models": models,
    }


def _model(provider, model_id):
    return {
        "id": model_id,
        "label": model_id,
        "provider": provider,
        "tier": "mid",
    }


class MobileModelCatalogueTests(unittest.TestCase):
    def build(self, keys, models, account=None):
        with (
            patch.object(catalogue, "load_api_keys", return_value=keys),
            patch.object(
                catalogue,
                "has_key",
                side_effect=lambda provider: bool(str(keys.get(provider) or "").strip()),
            ),
            patch.object(catalogue, "get_all_models", return_value=models),
            patch.object(catalogue, "invalidate_ollama_models_cache"),
            patch.object(catalogue, "account_snapshot", return_value=account or _account()),
            patch.object(catalogue, "get_reasoning_options", return_value=[]),
            patch.object(catalogue, "resolve_reasoning_option_id", return_value="auto"),
            patch.object(catalogue, "model_has_vision", return_value=False),
            patch.object(catalogue, "load_settings", return_value={}),
        ):
            return catalogue.build_catalogue()

    def test_unconfigured_api_is_omitted_and_configured_api_is_included(self):
        models = [_model("openai", "gpt"), _model("deepseek", "deep")]
        result = self.build({"deepseek": "key"}, models)
        self.assertEqual(
            [item["providerId"] for item in result["providers"]], ["deepseek"]
        )

    def test_static_models_cannot_expose_any_unconfigured_api_provider(self):
        models = [
            _model(provider, f"{provider}-static")
            for provider in catalogue.API_PROVIDERS
        ]
        result = self.build(
            {"anthropic": "", "xai": "   ", "mcp:other": "not-an-ai-key"},
            models,
        )
        self.assertEqual(result["providers"], [])
        self.assertEqual(result["models"], [])

    def test_configured_provider_is_present_once_after_repeated_refresh(self):
        models = [
            _model("openai", "gpt"),
            _model("openai", "gpt"),
            _model("anthropic", "claude"),
        ]
        first = self.build({"openai": "key"}, models)
        second = self.build({"openai": "key"}, models)
        for result in (first, second):
            self.assertEqual(
                [item["providerId"] for item in result["providers"]], ["openai"]
            )
            self.assertEqual([item["id"] for item in result["models"]], ["gpt"])

    def test_chatgpt_requires_connected_usable_models(self):
        account_model = {
            "id": "account-model",
            "displayName": "Account",
            "providerId": "chatgpt_account",
            "provider": "chatgpt_account",
            "inputModalities": ["text"],
            "hidden": False,
            "usable": True,
            "selectable": True,
            "vision": False,
            "reasoning": {"supported": False, "options": [], "selected": "auto"},
        }
        disconnected = self.build({}, [], _account())
        connected = self.build({}, [], _account(connected=True, models=[account_model]))
        zero_models = self.build({}, [], _account(connected=True, models=[]))
        self.assertEqual(disconnected["providers"], [])
        self.assertEqual(connected["providers"][0]["providerId"], "chatgpt_account")
        self.assertEqual(zero_models["providers"], [])

    def test_mobile_account_quota_is_remaining_only_and_sanitized(self):
        provider = MagicMock()
        provider.status.return_value = {
            "available": True,
            "runtimeAvailable": True,
            "connected": True,
            "account": {"email": "safe@example.test", "planType": "plus"},
            "models": [],
            "selectedModel": "",
            "error": "",
        }
        provider.rate_limits.return_value = {
            "rateLimits": {
                "primary": {"usedPercent": 0, "windowDurationMins": 300},
                "secondary": {"usedPercent": 21, "windowDurationMins": 10080},
            },
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "primary": {"usedPercent": 0, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 0, "windowDurationMins": 10080},
                },
            },
            "credential": "must-not-leak",
        }
        with patch(
            "osia.chatgpt_account.provider.get_account_provider",
            return_value=provider,
        ):
            snapshot = catalogue.account_snapshot(refresh_models=False)

        self.assertEqual(snapshot["quotaWindows"], [
            {"label": "5 h", "remainingPercent": 100},
            {"label": "Weekly", "remainingPercent": 79},
        ])
        self.assertNotIn("usagePercent", snapshot)
        encoded = json.dumps(snapshot)
        self.assertNotIn("usedPercent", encoded)
        self.assertNotIn("bengalfox", encoded.lower())
        self.assertNotIn("must-not-leak", encoded)

    def test_local_provider_requires_detected_models(self):
        absent = self.build({}, [])
        present = self.build({}, [_model("ollama", "llama3")])
        self.assertEqual(absent["providers"], [])
        self.assertEqual(present["providers"][0]["providerId"], "ollama")
        self.assertEqual(present["providers"][0]["modelCount"], 1)

    def test_explicit_catalogue_refresh_preserves_local_cache_invalidation(self):
        with (
            patch.object(catalogue, "load_api_keys", return_value={}),
            patch.object(catalogue, "get_all_models", return_value=[]),
            patch.object(catalogue, "invalidate_ollama_models_cache") as invalidate,
            patch.object(catalogue, "account_snapshot", return_value=_account()),
            patch.object(catalogue, "load_settings", return_value={}),
        ):
            catalogue.build_catalogue(refresh_local=True, refresh_account=False)
        invalidate.assert_called_once_with()

    def test_catalogue_schema_contains_no_secret_or_path_fields(self):
        result = self.build(
            {"openai": "super-secret-key"}, [_model("openai", "gpt")]
        )
        encoded = json.dumps(result).lower()
        for forbidden in (
            "super-secret-key", "access_token", "refresh_token", "api_key",
            "threadid", "auth.json", "workspace_dir", "filesystem",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_exact_provider_model_selection_is_validated_and_persisted(self):
        current = {
            "models": [
                {"providerId": "deepseek", "id": "shared"},
                {"providerId": "openai", "id": "shared"},
            ]
        }
        writes = []
        with (
            patch.object(catalogue, "build_catalogue", return_value=current),
            patch.object(catalogue, "set_setting", side_effect=lambda k, v: writes.append((k, v))),
        ):
            self.assertTrue(catalogue.validate_and_select("deepseek", "shared"))
            self.assertFalse(catalogue.validate_and_select("groq", "shared"))
        self.assertEqual(writes, [
            ("selected_model", "shared"),
            ("selected_model_provider", "deepseek"),
        ])

    def test_reasoning_is_validated_and_chatgpt_key_is_namespaced(self):
        current = {
            "models": [{
                "providerId": "chatgpt_account",
                "id": "shared",
                "reasoning": {"options": [{"id": "auto"}, {"id": "high"}]},
            }]
        }
        saved = []
        with (
            patch.object(catalogue, "build_catalogue", return_value=current),
            patch.object(catalogue, "get_reasoning_settings", return_value={"models": {}}),
            patch.object(catalogue, "set_reasoning_settings", side_effect=saved.append),
        ):
            self.assertTrue(
                catalogue.validate_and_set_reasoning(
                    "chatgpt_account", "shared", "high"
                )
            )
            self.assertFalse(
                catalogue.validate_and_set_reasoning(
                    "chatgpt_account", "shared", "unsupported"
                )
            )
        self.assertEqual(saved[0]["models"], {"chatgpt_account:shared": "high"})

    def test_api_key_change_listener_receives_provider_id_only(self):
        events = []
        unsubscribe = config.add_api_key_change_listener(events.append)
        try:
            with (
                patch.object(config, "load_api_keys", return_value={}),
                patch.object(config, "save_api_keys"),
            ):
                config.set_api_key("openai", "secret-value")
        finally:
            unsubscribe()
        self.assertEqual(events, ["openai"])
        self.assertNotIn("secret-value", events)

    def test_provider_login_logout_changes_notify_live_listeners(self):
        client = MagicMock()
        client.wait_for_login.return_value = {"connected": False}
        provider = ChatGPTAccountProvider(client=client)
        events = []
        unsubscribe = add_account_change_listener(lambda: events.append("changed"))
        try:
            provider.wait_for_login("login", timeout=1)
            provider.cancel_login()
            provider.logout()
        finally:
            unsubscribe()
        self.assertEqual(events, ["changed", "changed", "changed"])

    def test_loopback_bridge_fans_sanitized_catalogue_and_account(self):
        from osia.server import server

        class Client:
            def __init__(self):
                self.frames = []

            async def send_json(self, frame):
                self.frames.append(frame)

        account = _account(connected=True, models=[])
        safe = {
            "version": 1,
            "providers": [],
            "models": [],
            "current": "",
            "currentProvider": "",
            "account": account,
        }
        client = Client()
        server._WS_CLIENTS.add(client)
        server._MOBILE_CATALOGUE_BROADCAST_LOCK = None
        try:
            with patch.object(catalogue, "build_catalogue", return_value=safe):
                asyncio.run(
                    server._broadcast_mobile_catalogue(
                        refresh_account=True,
                        include_account_state=True,
                    )
                )
        finally:
            server._WS_CLIENTS.discard(client)
            server._MOBILE_CATALOGUE_BROADCAST_LOCK = None
        self.assertEqual(
            [frame["type"] for frame in client.frames],
            ["models_list", "chatgpt_account_state"],
        )
        encoded = json.dumps(client.frames).lower()
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("access_token", encoded)

    def test_catalogue_loopback_route_rejects_non_loopback_client(self):
        from osia.server import server

        request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"))
        result = asyncio.run(server.internal_mobile_catalogue_changed(request))
        self.assertEqual(result, {"ok": False, "error": "forbidden"})


if __name__ == "__main__":
    unittest.main()
