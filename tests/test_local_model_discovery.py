"""Local inventory uses exact runtime identifiers and a read-only disk fallback."""

import json
from types import SimpleNamespace

import pytest

from osia import config, models
from osia import local_model_discovery as discovery
from osia.mobile_model_catalogue import _mobile_model, build_model_inventory


def _server(monkeypatch, payloads):
    calls = []

    def get(session, url, **kwargs):
        calls.append(url)
        assert session.trust_env is False
        assert kwargs["allow_redirects"] is False
        payload = payloads.get(url)
        return SimpleNamespace(status_code=200 if payload is not None else 404, json=lambda: payload)

    monkeypatch.setattr(discovery.requests.Session, "get", get)
    return calls


def test_ollama_server_returns_all_exact_tags_and_namespaces(monkeypatch):
    base = "http://127.0.0.1:11435/v1"
    url = "http://127.0.0.1:11435/api/tags"
    calls = _server(monkeypatch, {url: {"models": [
        {"model": "qwen3:4b"}, {"name": "custom/model:q8"}, {"model": "qwen3:4b"},
        {"name": "hosted:cloud", "remote_host": "https://example.invalid"}, None,
    ]}})

    found = discovery.query_ollama_models(base)

    assert calls == [url]
    assert [m["id"] for m in found] == ["qwen3:4b", "custom/model:q8"]
    assert all(m["available"] and m["provider"] == "ollama" for m in found)


def test_configured_openai_compatible_local_endpoint_is_also_discovered(monkeypatch):
    base = "http://127.0.0.1:17808/local/v1"
    calls = _server(monkeypatch, {base + "/models": {"data": [{"id": "custom-inference-id"}]}})
    monkeypatch.setattr(config, "get_setting", lambda *args: {"ollama": {"primary": base}})
    assert models._query_ollama_models()[0]["id"] == "custom-inference-id"
    assert calls == ["http://127.0.0.1:17808/local/api/tags", base + "/models"]


@pytest.mark.parametrize(("value", "expected"), [
    ("0.0.0.0:11499", "http://127.0.0.1:11499/v1"),
    ("[::]:11499", "http://[::1]:11499/v1"),
    ("127.0.0.1:11499", "http://127.0.0.1:11499/v1"),
    ("example.invalid:11499", "http://localhost:11434/v1"),
])
def test_ollama_environment_and_inference_use_the_same_local_port(monkeypatch, value, expected):
    monkeypatch.setenv("OLLAMA_HOST", value)
    monkeypatch.setattr(config, "get_setting", lambda *args: {})
    assert models.endpoint_for_provider("ollama") == expected


def test_offline_ollama_reports_installed_manifests_as_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    _server(monkeypatch, {})
    for path in (
        "registry.ollama.ai/library/qwen3/4b", "registry.ollama.ai/user/model/q4_k_m",
        "other.example/org/model/latest", "registry.ollama.ai/library/broken/latest",
    ):
        manifest = tmp_path / "manifests" / path
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("incomplete" if "broken" in path else json.dumps({"layers": [{"size": 50}]}))

    rows = discovery.query_ollama_models("http://127.0.0.1:11434/v1")

    assert {row["id"] for row in rows} == {"qwen3:4b", "user/model:q4_k_m", "other.example/org/model:latest"}
    assert all(not row["available"] for row in rows)
    calls = _server(monkeypatch, {"http://127.0.0.1:11434/api/tags": {"models": []}})
    assert discovery.query_ollama_models("http://127.0.0.1:11434/v1") == []
    assert len(calls) == 1


def test_connection_failure_uses_disk_without_probing_another_path(monkeypatch, tmp_path):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setenv("LMSTUDIO_HOME", str(tmp_path))
    calls = []

    def get(session, url, **kwargs):
        calls.append(url)
        raise discovery.requests.ConnectionError("loopback fixture unavailable")

    monkeypatch.setattr(discovery.requests.Session, "get", get)
    assert discovery.query_ollama_models("http://127.0.0.1:11434/v1") == []
    assert discovery.query_lmstudio_models("http://127.0.0.1:1234/v1") == []
    assert calls == ["http://127.0.0.1:11434/api/tags", "http://127.0.0.1:1234/api/v1/models"]


def test_lmstudio_projects_chat_models_and_exact_loaded_aliases(monkeypatch):
    root = "http://127.0.0.1:1234"
    _server(monkeypatch, {
        root + "/api/v1/models": {"models": [
            {"type": "llm", "key": "vendor/vision", "display_name": "Vision Model",
             "max_context_length": 32768, "capabilities": {"vision": True, "trained_for_tool_use": True},
             "loaded_instances": [{"id": "vision-alias", "config": {"context_length": 4096}}]},
            {"type": "llm", "key": "vendor/unloaded", "loaded_instances": []},
            {"type": "llm", "key": "vendor/jit", "loaded_instances": []},
            {"type": "embedding", "key": "vendor/embed", "loaded_instances": [{"id": "my-embedding"}]},
            {"type": "decision", "key": "vendor/decision"},
        ]},
        root + "/v1/models": {"data": [{"id": mid} for mid in (
            "vision-alias", "vendor/jit", "vendor/embed", "my-embedding", "vendor/decision",
        )]},
    })

    found = {entry["id"]: entry for entry in discovery.query_lmstudio_models(root + "/v1")}

    assert set(found) == {"vendor/vision", "vendor/unloaded", "vendor/jit", "vision-alias"}
    assert not found["vendor/unloaded"]["available"]
    assert found["vision-alias"]["context_window"] == 4096
    assert found["vision-alias"]["vision"] and found["vision-alias"]["tool_calls"]
    assert found["vendor/jit"]["available"]
    inventory = build_model_inventory(list(found.values()), set())
    phone = {m["id"]: m for m in map(_mobile_model, inventory)}
    assert phone["vision-alias"]["selectable"] and phone["vision-alias"]["providerId"] == "lmstudio"
    assert not phone["vendor/unloaded"]["selectable"]


def test_lmstudio_offline_index_checks_files_and_merges_legacy_api_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("LMSTUDIO_HOME", str(tmp_path))
    index = tmp_path / ".internal" / "model-index-cache.json"
    index.parent.mkdir()
    model_path = tmp_path / "fixture.gguf"
    model_path.touch()
    entry = {"domain": "llm", "indexedModelIdentifier": "vendor/repo/fixture.gguf",
             "defaultIdentifier": "fixture", "displayName": "Fixture Chat",
             "entryPoint": {"absPath": str(model_path)}}
    index.write_text(json.dumps({"models": [
        entry, {**entry, "domain": "embedding", "indexedModelIdentifier": "embedding"},
        {**entry, "indexedModelIdentifier": "deleted", "entryPoint": {"absPath": str(tmp_path / "missing.gguf")}},
    ]}), encoding="utf-8")
    root = "http://127.0.0.1:1234/v1"
    _server(monkeypatch, {})
    offline = discovery.query_lmstudio_models(root)
    assert [(m["id"], m["available"]) for m in offline] == [("vendor/repo/fixture.gguf", False)]

    _server(monkeypatch, {root + "/models": {"data": [{"id": "fixture"}]}})
    live = discovery.query_lmstudio_models(root)
    assert [(m["id"], m["label"], m["available"]) for m in live] == [("fixture", "Fixture Chat", True)]
