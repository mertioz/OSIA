"""The official Claude Models API is paginated; catalogs remain discovery data."""

from types import SimpleNamespace

import pytest

from osia import config, models


def test_all_claude_model_pages_keep_auth_and_exact_ids(monkeypatch):
    calls = []
    pages = iter([
        {"data": [{"id": "claude-first", "display_name": "First"}], "has_more": True, "last_id": "claude-first"},
        {"data": [{"id": "claude-second", "display_name": "Second"}], "has_more": False, "last_id": "claude-second"},
    ])

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: next(pages))

    monkeypatch.setattr(models.requests, "get", get)
    monkeypatch.setattr(config, "get_setting", lambda name, default=None: "wrkspc_fixture" if name == "anthropic_workspace_id" else default)
    result = models.fetch_remote_models("anthropic", "fixture-key")
    assert [model["id"] for model in result] == ["claude-first", "claude-second"]
    assert len(calls) == 2
    assert calls[1][1]["params"]["after_id"] == "claude-first"
    assert all(call[1]["headers"]["x-api-key"] == "fixture-key" for call in calls)
    assert all(call[1]["headers"]["anthropic-workspace-id"] == "wrkspc_fixture" for call in calls)


@pytest.mark.parametrize("last_id", [None, "", "repeated"])
def test_corrupt_claude_pagination_ends_only_this_discovery_operation(monkeypatch, last_id):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        assert len(calls) <= 2
        return SimpleNamespace(status_code=200, json=lambda: {
            "data": [{"id": "one"}], "has_more": True, "last_id": last_id,
        })

    monkeypatch.setattr(models.requests, "get", get)
    assert models.fetch_remote_models("anthropic", "fixture-key") == []


def test_failed_later_page_is_not_published_as_a_complete_catalog(monkeypatch):
    pages = iter([
        SimpleNamespace(status_code=200, json=lambda: {
            "data": [{"id": "one"}], "has_more": True, "last_id": "one",
        }),
        SimpleNamespace(status_code=403),
    ])
    monkeypatch.setattr(models.requests, "get", lambda *args, **kwargs: next(pages))
    assert models.fetch_remote_models("anthropic", "fixture-key") == []
