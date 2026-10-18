"""Account catalog, selected-model fidelity and restart context fixtures."""

import json
from unittest.mock import Mock, patch

import pytest

from osia.chatgpt_account.app_server_client import AppServerClient
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia import mobile_model_catalogue as catalogue
from tools.test_chatgpt_account_integration import _ProviderClient


def test_saved_model_is_never_replaced_by_a_catalog_default():
    provider = ChatGPTAccountProvider(client=_ProviderClient())
    with patch("osia.chatgpt_account.provider.get_setting", return_value="selected-exact-model"):
        assert provider.selected_model([{"id": "other", "isDefault": True}]) == "selected-exact-model"


def test_new_account_selection_can_use_reported_default():
    provider = ChatGPTAccountProvider(client=_ProviderClient())
    with patch("osia.chatgpt_account.provider.get_setting", return_value=None):
        assert provider.selected_model([{"id": "first"}, {"id": "default", "isDefault": True}]) == "default"


def test_disconnected_status_does_not_expose_cached_models_to_mobile():
    client = _ProviderClient()
    client.health_check = lambda: {"runtimeVersion": "fixture"}
    client.get_account_status = lambda: {"connected": False}
    provider = ChatGPTAccountProvider(client=client)
    provider._models = [{"id": "prior-account-model"}]
    with (
        patch("osia.chatgpt_account.provider.get_setting", return_value=None),
        patch("osia.chatgpt_account.provider.get_account_provider", return_value=provider),
    ):
        snapshot = catalogue.account_snapshot(refresh_models=False)
    assert snapshot["connected"] is False
    assert snapshot["models"] == []
    assert snapshot["selectedModel"] == ""
    assert provider.cached_models() == []


def test_account_update_invalidates_old_quota_and_thread_generation(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client._rate_limits_cache = {"rateLimits": {"primary": {"usedPercent": 99}}}
    client._rate_limits_pending_update = {"rateLimits": {"primary": {"usedPercent": 88}}}
    before = getattr(client, "account_generation", 0)
    client._dispatch_notification("account/updated", {"authMode": "chatgpt", "planType": "plus"})
    assert client._rate_limits_cache is None
    assert client._rate_limits_pending_update == {}
    assert client.account_generation > before


def test_models_returning_after_account_switch_are_not_published():
    client = _ProviderClient()
    client.account_generation = 0

    def list_models():
        client.account_generation += 1
        return [{"id": "old-account-model"}]

    client.list_models = list_models
    provider = ChatGPTAccountProvider(client=client)
    assert provider.list_models(force=True) == []
    assert provider.cached_models() == []


def test_quota_returning_after_account_switch_is_not_published(tmp_path):
    client = AppServerClient(base_dir=tmp_path)

    def request(*_args, **_kwargs):
        client._dispatch_notification("account/updated", {"authMode": "chatgpt"})
        return {"rateLimits": {"primary": {"usedPercent": 99, "windowDurationMins": 300}}}

    client._request = request
    assert client.get_rate_limits() == {}


@pytest.mark.parametrize("change", ["restart", "account_switch"])
def test_runtime_change_rebuilds_with_full_canonical_context_and_same_model(change):
    client = _ProviderClient()
    client.account_generation = 0
    restart = {"requested": False}

    def start():
        if restart["requested"]:
            client.generation += 1
            restart["requested"] = False

    client.start = start
    provider = ChatGPTAccountProvider(client=client)
    first = [{"role": "system", "content": "OSIA"}, {"role": "user", "content": "first"}]
    with patch("osia.chatgpt_account.provider._canonical_native_tools", return_value=[]):
        provider.complete(conversation_id="same-conversation", model="exact-model", messages=first)
        if change == "restart":
            restart["requested"] = True
        else:
            client.account_generation += 1
        provider.complete(
            conversation_id="same-conversation", model="exact-model",
            messages=first + [{"role": "assistant", "content": "account reply"}, {"role": "user", "content": "next"}],
        )
    assert len(client.started) == 2
    assert all(item["model"] == "exact-model" for item in client.started + client.turns)
    assert "first" in client.turns[-1]["text"]
    assert "account reply" in client.turns[-1]["text"]
    assert "next" in client.turns[-1]["text"]


def test_settings_probe_never_overwrites_an_explicit_selection():
    from osia.gui.main_popup import _SettingsPanel

    provider = Mock()
    provider.status.return_value = {"connected": True, "models": [{"id": "other"}], "selectedModel": "other"}
    provider.rate_limits.return_value = {}
    with patch("osia.gui.main_popup.get_setting", return_value="selected-exact-model"):
        _SettingsPanel._chatgpt_status_with_usage(provider)
    provider.set_selected_model.assert_not_called()


def test_mobile_account_projection_never_serializes_provider_credentials():
    raw = {
        "available": True, "connected": True, "runtimeAvailable": True,
        "selectedModel": "exact-model", "models": [{"id": "exact-model", "access_token": "fixture-private"}],
        "account": {"email": "person@example.test", "planType": "plus", "refresh_token": "fixture-private"},
        "access_token": "fixture-private", "error": "",
    }
    with patch.object(catalogue, "_account_status_with_usage", return_value=raw):
        snapshot = catalogue.account_snapshot()
    assert "fixture-private" not in json.dumps(snapshot)
    assert snapshot["selectedModel"] == "exact-model"
