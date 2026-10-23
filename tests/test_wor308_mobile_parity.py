from __future__ import annotations

import json
from types import SimpleNamespace

from osia import config, personality
from osia.gui.theme import PALETTES
from osia.memory import MemoryStore, add_memory_change_listener
from osia.server import server
from osia.voice import model_inventory


def test_mobile_config_snapshot_contains_presence_metadata_only(monkeypatch):
    fallback_secret = "fake-fallback"
    provider_secret = "fake-provider"
    mcp_secret = "fake-mcp"
    settings = {
        "selected_model_provider": "openai",
        "selected_model": "gpt-5.5",
        "ui_theme": "azure",
        "api_key_fallbacks": {"openai": [fallback_secret, "second-secret"]},
        "mcp_servers": [
            {
                "id": "github",
                "name": "GitHub",
                "transport": "stdio",
                "auth_type": "token",
                "env": {"GITHUB_TOKEN": mcp_secret},
                "headers": {"Authorization": f"Bearer {mcp_secret}"},
            }
        ],
        "cron_jobs": [],
        "reasoning": {"default": "auto", "providers": {}, "models": {}},
    }

    monkeypatch.setattr(config, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(
        config,
        "get_setting",
        lambda key, default=None: settings.get(key, default),
    )
    monkeypatch.setattr(
        config,
        "load_api_keys",
        lambda: {"openai": provider_secret, "mcp:github": mcp_secret},
    )
    monkeypatch.setattr(server, "_gmail_rest_config_snapshot", lambda: {})
    monkeypatch.setattr(
        personality,
        "resolve_personality",
        lambda: SimpleNamespace(text="safe personality", identifier="default", fallback_reason=None),
    )
    monkeypatch.setattr(personality, "discover_skills", lambda: [])
    monkeypatch.setattr(model_inventory, "available_whisper_model_sizes", lambda: {"small"})

    snapshot = server._build_config_snapshot()
    serialized = json.dumps(snapshot)

    assert snapshot["api_keys_set"]["openai"] is True
    assert snapshot["api_keys_set"]["mcp:github"] is True
    assert snapshot["api_key_fallbacks_set"] == {"openai": [True, True]}
    assert snapshot["stt_local_models_available"] == ["small"]
    assert "personal_browser_preference" not in snapshot
    assert "personal_browser_instance_hint" not in snapshot
    assert "api_key_fallbacks" not in snapshot
    assert "mcp_tokens" not in snapshot
    assert "api_keys" not in snapshot
    for secret in (fallback_secret, provider_secret, mcp_secret, "private-browser-target"):
        assert secret not in serialized


def test_legacy_api_keys_reply_contains_status_without_values():
    frame = server._safe_api_key_status_frame(
        {"openai": "sk-never-return-this", "mcp:github": "ghp-never-return-this"},
        ["openai", "deepseek"],
    )

    assert frame == {
        "type": "api_keys",
        "keys": {"openai": "", "deepseek": ""},
        "keys_set": {"openai": True, "deepseek": False},
    }
    assert "sk-never-return-this" not in json.dumps(frame)
    assert "ghp-never-return-this" not in json.dumps(frame)


def test_settings_change_listeners_receive_keys_not_values(monkeypatch):
    written = {}
    changed = []
    unsubscribe = config.add_setting_change_listener(changed.append)
    monkeypatch.setattr(config, "load_settings", lambda: {})
    monkeypatch.setattr(config, "save_settings", lambda value: written.update(value))

    try:
        config.set_setting("ui_theme", "amber")
    finally:
        unsubscribe()

    assert written["ui_theme"] == "amber"
    assert changed == ["ui_theme"]


def test_memory_store_change_listener_receives_canonical_snapshots(tmp_path):
    store = MemoryStore(tmp_path / "memory.json")
    snapshots = []
    unsubscribe = add_memory_change_listener(snapshots.append)

    try:
        store.upsert_entry({
            "id": "mem_parity_test",
            "type": "fact",
            "category": "other",
            "key": "tea",
            "title": "Tea preference",
            "value": {"kind": "text", "data": "green"},
        })
        assert store.delete_entry("mem_parity_test") is True
    finally:
        unsubscribe()

    assert len(snapshots) == 2
    assert [entry["id"] for entry in snapshots[0]["entries"]] == [
        "mem_parity_test"
    ]
    assert snapshots[1]["entries"] == []


def test_pc_theme_registry_is_the_canonical_mobile_theme_set():
    assert set(PALETTES) == {"osia", "azure", "emerald", "rose", "amber", "neutral"}
    assert PALETTES["osia"].background == "#0d0b12"
    assert PALETTES["osia"].primary == "#c4b5f5"
