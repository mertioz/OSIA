"""Focused WOR-221 skill activation and canonical snapshot regressions."""

from __future__ import annotations

import json

import pytest

from osia import config, personality


def _skill(identifier: str, marker: str, *, dependency: str = "") -> str:
    required = f"## Required tools\n- {dependency}\n" if dependency else ""
    return (
        f"# Skill: {identifier}\n"
        "## Description\nA deterministic reusable instruction bundle.\n"
        f"{required}"
        f"## Instructions\n{marker}\n"
    )


class _Checkbox:
    def __init__(self, checked: bool) -> None:
        self._checked = checked
        self.signal_blocks: list[bool] = []

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        self._checked = checked

    def blockSignals(self, blocked: bool) -> None:
        self.signal_blocks.append(blocked)


@pytest.fixture()
def persistent_registry(tmp_path, monkeypatch):
    private = tmp_path / "private"
    skills = private / "skills"
    skills.mkdir(parents=True)
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"skills_enabled": {"valid.md": False}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(tmp_path / "api_keys.json"))
    monkeypatch.setattr(personality, "OSIA_DIR", str(private))
    monkeypatch.setattr(personality, "PERSONALITY_FILE", str(private / "personality.md"))
    monkeypatch.setattr(personality, "SKILLS_DIR", str(skills))
    monkeypatch.setattr(personality, "get_setting", config.get_setting)
    monkeypatch.setattr(personality, "set_setting", config.set_setting)
    config._settings_cache_signature = None
    config._settings_cache_data = {}
    yield skills, settings_file
    config._settings_cache_signature = None
    config._settings_cache_data = {}


def test_valid_activation_survives_refresh_restart_edit_and_disable(
    persistent_registry,
):
    skills, settings_file = persistent_registry
    path = skills / "valid.md"
    path.write_text(_skill("valid", "WOR221_ENABLED_ONCE"), encoding="utf-8")

    before = personality.get_skill_definition("valid.md")
    assert before is not None
    assert before.available is True
    assert before.enabled is False

    from osia.gui.main_popup import _OsiaPanel

    checkbox = _Checkbox(True)
    _OsiaPanel._on_skill_toggle("valid.md", checkbox)
    assert checkbox.isChecked() is True
    assert checkbox.signal_blocks == []
    immediate = personality.get_skill_definition("valid.md")
    assert immediate is not None and immediate.enabled is True
    first_revision = config.get_setting("skills_state_revision")
    assert first_revision == 1

    # Reopening/restarting clears the read cache and reconstructs from disk.
    config._settings_cache_signature = None
    config._settings_cache_data = {}
    restarted = personality.get_skill_definition("valid.md")
    assert restarted is not None and restarted.enabled is True

    personality.write_skill(
        "valid.md",
        _skill("valid", "WOR221_ENABLED_ONCE after an authorized edit."),
    )
    edited = personality.get_skill_definition("valid.md")
    assert edited is not None and edited.enabled is True
    assert config.get_setting("skills_state_revision") == first_revision + 1

    from osia import agent

    prompt = agent.get_system_prompt("en", "test-model", "ollama")
    assert prompt.count("WOR221_ENABLED_ONCE") == 1

    assert personality.set_skill_enabled("valid.md", False) is False
    disabled = personality.get_skill_definition("valid.md")
    assert disabled is not None and disabled.enabled is False
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["skills_enabled"]["valid.md"] is False
    assert persisted["skills_state_revision"] == first_revision + 2


def test_snapshot_orders_mutations_and_invalid_skills_fail_closed(
    persistent_registry,
    monkeypatch,
):
    skills, _settings_file = persistent_registry
    (skills / "valid.md").write_text(
        _skill("valid", "WOR221_SNAPSHOT_SKILL"),
        encoding="utf-8",
    )
    (skills / "invalid.md").write_text("unstructured prose", encoding="utf-8")
    (skills / "missing.md").write_text(
        _skill(
            "missing",
            "WOR221_MISSING_DEPENDENCY",
            dependency="wor221_absent_tool",
        ),
        encoding="utf-8",
    )

    from osia.server import server

    monkeypatch.setattr(config, "load_api_keys", lambda: {})
    monkeypatch.setattr(server, "_gmail_rest_config_snapshot", lambda: {})

    stale = server._build_config_snapshot()
    assert stale["skills_state_revision"] == 0
    assert personality.set_skill_enabled("valid.md", True) is True
    current = server._build_config_snapshot()
    assert current["skills_state_revision"] > stale["skills_state_revision"]
    assert current["skills_enabled"]["valid.md"] is True
    by_name = {skill["name"]: skill for skill in current["skills"]}
    assert by_name["valid.md"]["enabled"] is True

    from osia.gui.main_popup import _OsiaPanel

    invalid_checkbox = _Checkbox(True)
    _OsiaPanel._on_skill_toggle("invalid.md", invalid_checkbox)
    assert invalid_checkbox.isChecked() is False
    assert invalid_checkbox.signal_blocks == [True, False]
    assert personality.set_skill_enabled("missing.md", True) is False
    rejected = server._build_config_snapshot()
    by_name = {skill["name"]: skill for skill in rejected["skills"]}
    assert by_name["invalid.md"] == {
        "name": "invalid.md",
        "enabled": False,
        "text": "unstructured prose",
        "available": False,
        "status": "invalid_metadata",
    }
    assert by_name["missing.md"]["enabled"] is False
    assert by_name["missing.md"]["available"] is False
    assert by_name["missing.md"]["status"] == "missing_dependency"
