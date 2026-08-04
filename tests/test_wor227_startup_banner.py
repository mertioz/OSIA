import time


def test_startup_banner_uses_configured_provider_without_ollama_probe(
    monkeypatch, capsys,
):
    from osia import config, models
    from osia.server import server

    monkeypatch.setattr(
        config,
        "load_settings",
        lambda: {
            "selected_model": "configured-model-fixture",
            "selected_model_provider": "configured-provider-fixture",
        },
    )
    monkeypatch.setattr(
        models,
        "_query_ollama_models",
        lambda: (_ for _ in ()).throw(AssertionError("startup probe")),
    )

    started = time.monotonic()
    server._log_active_model()
    elapsed = time.monotonic() - started

    output = capsys.readouterr().out
    assert elapsed < 0.5
    assert "configured-model-fixture" in output
    assert "configured-provider-fixture" in output


def test_startup_banner_uses_cached_local_model_while_probe_is_hung(
    monkeypatch, capsys,
):
    from osia import config, models
    from osia.server import server

    monkeypatch.setattr(
        config,
        "load_settings",
        lambda: {"selected_model": "cached-local-fixture"},
    )
    monkeypatch.setattr(
        models,
        "_OLLAMA_CACHE",
        [{"id": "cached-local-fixture", "provider": "ollama"}],
    )
    monkeypatch.setattr(models, "_OLLAMA_CACHE_AT", time.monotonic())

    def hung_probe():
        time.sleep(5.0)
        return []

    monkeypatch.setattr(models, "_query_ollama_models", hung_probe)

    started = time.monotonic()
    server._log_active_model()
    elapsed = time.monotonic() - started

    output = capsys.readouterr().out
    assert elapsed < 0.5
    assert "cached-local-fixture" in output
    assert "Provider:     ollama" in output


def test_explicit_later_ollama_discovery_still_queries(monkeypatch):
    from osia import models

    discovered = [{"id": "explicit-local-fixture", "provider": "ollama"}]
    calls = []
    monkeypatch.setattr(models, "_OLLAMA_CACHE", None)
    monkeypatch.setattr(models, "_OLLAMA_CACHE_AT", 0.0)
    monkeypatch.setattr(models, "_OLLAMA_REFRESH_IN_FLIGHT", False)
    monkeypatch.setattr(
        models,
        "_query_ollama_models",
        lambda: calls.append(True) or discovered,
    )

    assert models.get_ollama_models() == discovered
    assert calls == [True]
