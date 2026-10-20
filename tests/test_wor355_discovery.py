"""Free discovery contracts and catalogue provenance; no inference requests."""
from types import SimpleNamespace as NS

import pytest

from osia import models
from osia import mobile_model_catalogue as mobile


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    models.invalidate_remote_models()
    monkeypatch.setattr(models, "_DISCOVERY_INVALIDATED", set())
    monkeypatch.setattr("osia.utils.key_chain.iter_keys", lambda provider: [])
    monkeypatch.setattr(models, "get_ollama_models", lambda: [])
    monkeypatch.setattr(models, "cached_ollama_models", lambda: [])
    yield
    models.invalidate_remote_models()


def reply(data, status=200):
    return NS(status_code=status, json=lambda: data)


def test_google_header_pagination_and_media_exclusion(monkeypatch):
    calls = []
    pages = iter([
        {"models": [{"name": "models/gemini-4-argon", "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1048576}], "nextPageToken": "next"},
        {"models": [{"name": "models/gemini-embedding", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/gemini-tts", "supportedGenerationMethods": ["generateContent"]}]},
    ])
    def get(url, **kwargs):
        calls.append(kwargs)
        return reply(next(pages))
    monkeypatch.setattr(models.requests, "get", get)
    found = models.fetch_remote_models("google", "fixture-key")
    assert [m["id"] for m in found] == ["gemini-4-argon"]
    assert found[0]["context_window"] == 1048576
    assert calls[1]["params"]["pageToken"] == "next"
    assert calls[0]["headers"] == {"x-goog-api-key": "fixture-key"}


def test_mistral_capability_filter_and_metadata(monkeypatch):
    monkeypatch.setattr(models.requests, "get", lambda *a, **kw: reply({"data": [
        {"id": "mistral-discovered", "capabilities": {"completion_chat": True, "function_calling": True, "vision": True}, "max_context_length": 1000000},
        {"id": "mistral-embed", "capabilities": {"completion_chat": False}},
    ]}))
    result = models.discover_all_remote_models({"mistral": "fixture-key"})
    assert [m["id"] for m in result] == ["mistral-discovered"]
    assert models.model_has_vision("mistral", "mistral-discovered")
    assert models.model_metadata("mistral", "mistral-discovered")["tool_calls"] is True


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_errors_and_empty_do_not_resurrect_static_models(monkeypatch, status):
    monkeypatch.setattr(models.requests, "get", lambda *a, **kw: reply({"data": []}, status))
    assert models.discover_all_remote_models({"openai": "fixture-key"}) == []
    assert not [m for m in models.get_all_models({}) if m["provider"] == "openai"]
    assert models.discovery_status()["openai"]["status"] == "error"
    monkeypatch.setattr(models.requests, "get", lambda *a, **kw: reply({"data": []}))
    models.discover_all_remote_models({"openai": "fixture-key"})
    assert models.discovery_status()["openai"]["status"] == "empty"


def test_fallback_is_unverified_and_mobile_cannot_select_it(monkeypatch):
    raw = models.get_all_models({})
    assert all(not m["available"] for m in raw)
    inventory = mobile.build_model_inventory(raw, {"openai"})
    assert inventory
    assert all(not mobile._mobile_model(entry)["selectable"] for entry in inventory)


def test_refresh_replaces_catalogue_and_preserves_exact_identity(monkeypatch):
    pages = iter([{"data": [{"id": "gpt-6.1-sol"}, {"id": "gpt-4.1"}, {"id": "text-embedding-3-large"}, {"id": "fake-model"}]},
                  {"data": [{"id": "gpt-4.1"}]}])
    monkeypatch.setattr(models.requests, "get", lambda *a, **kw: reply(next(pages)))
    models.discover_all_remote_models({"openai": "fixture-key"})
    assert [m["id"] for m in models.get_all_models({}) if m["provider"] == "openai"] == ["gpt-6.1-sol", "gpt-4.1"]
    models.discover_all_remote_models({"openai": "fixture-key"})
    assert [m["id"] for m in models.get_all_models({}) if m["provider"] == "openai"] == ["gpt-4.1"]


def test_rotation_uses_fallback_only_keys_and_never_rotates_404(monkeypatch):
    monkeypatch.setattr("osia.utils.key_chain.iter_keys", lambda provider: ["first", "second"] if provider == "deepseek" else [])
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        return reply({"data": [{"id": "deepseek-flash"}]}, 401 if len(calls) == 1 else 200)
    monkeypatch.setattr(models.requests, "get", get)
    assert models.discover_all_remote_models({})[0]["id"] == "deepseek-flash"
    assert calls == ["Bearer first", "Bearer second"]
    calls.clear()
    monkeypatch.setattr(models.requests, "get", lambda *a, **kw: calls.append(1) or reply({}, 404))
    models.discover_all_remote_models({})
    assert len(calls) == 1


def test_credentials_changed_during_discovery_reject_stale_callback(monkeypatch):
    def get(*args, **kwargs):
        models.invalidate_remote_models("openai")
        return reply({"data": [{"id": "gpt-6.1-sol"}]})
    monkeypatch.setattr(models.requests, "get", get)
    assert models.discover_all_remote_models({"openai": "fixture-key"}) == []
    assert "openai" not in models.discovery_status()


def test_stale_discovery_is_not_selectable(monkeypatch):
    monkeypatch.setattr(models.requests, "get", lambda *a, **kw: reply({"data": [{"id": "gpt-6.1-sol"}]}))
    models.discover_all_remote_models({"openai": "fixture-key"})
    monkeypatch.setattr(models.time, "time", lambda: 10**20)
    raw = models.get_all_models({})
    entry = next(m for m in raw if m["provider"] == "openai")
    assert entry["availability"] == "stale"
    assert entry["available"] is False
