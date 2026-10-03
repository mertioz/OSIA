from __future__ import annotations

import json
from pathlib import Path

import pytest

from osia import action_policy
from osia import config
from osia.mcp_client import registry


@pytest.fixture(autouse=True)
def _clear_confirmation_provider():
    registry.set_confirmation_provider(None)
    yield
    registry.set_confirmation_provider(None)
    with config._settings_cache_lock:
        config._settings_cache_signature = None
        config._settings_cache_data = {}


def _legacy_unsafe_settings(monkeypatch) -> None:
    values = {
        "mcp_confirm_writes": False,
        "mcp_allow_unattended_writes": True,
    }
    monkeypatch.setattr(
        config,
        "get_setting",
        lambda key, default=None: values.get(key, default),
    )


@pytest.mark.parametrize("tool_name", ["delete_file", "delete_directory", "run_command"])
def test_native_sensitive_actions_ignore_legacy_mcp_values_without_approval(
    monkeypatch, tmp_path, tool_name,
):
    _legacy_unsafe_settings(monkeypatch)
    prompts = []
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or False
    )
    # WOR-301: ordinary (non-OSIA, non-system) targets execute without any
    # approval; the legacy settings have no authority left.
    target = tmp_path / "ordinary-target"
    target.mkdir()
    arguments = {
        "delete_file": {"path": str(target / "note.txt")},
        "delete_directory": {"path": str(target), "recursive": False},
        "run_command": {"command": "echo private-value"},
    }[tool_name]

    error = action_policy.guard_tool_call(
        "Perform the requested local operation.", tool_name, arguments,
    )

    assert error is None
    assert prompts == []


class _Manager:
    def __init__(self):
        self.calls = []

    def call_tool(self, server_id, tool_name, arguments, *, timeout):
        self.calls.append((server_id, tool_name, arguments, timeout))
        return "ok"


def _mcp_wrapper(manager: _Manager, *, is_write: bool):
    return registry._make_wrapper(
        manager,
        "fixture-server",
        "write_item" if is_write else "read_item",
        is_write=is_write,
        input_schema={
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "required": ["item"],
            "additionalProperties": False,
        },
    )


def test_read_only_mcp_tool_runs_without_confirmation(monkeypatch):
    _legacy_unsafe_settings(monkeypatch)
    manager = _Manager()
    wrapper = _mcp_wrapper(manager, is_write=False)

    assert wrapper(item="fixture") == "ok"
    assert len(manager.calls) == 1


@pytest.mark.parametrize(
    "domain",
    [
        registry.ConfirmationDomain.NATIVE,
        registry.ConfirmationDomain.GMAIL,
        registry.ConfirmationDomain.MCP,
    ],
)
def test_every_sensitive_domain_proceeds_without_any_approver(monkeypatch, domain):
    _legacy_unsafe_settings(monkeypatch)
    prompts = []
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or None
    )

    allowed = registry.confirm_sensitive_action(
        domain,
        "fixture-origin",
        "fixture_tool",
        "Perform one sensitive action",
        call_arguments={"private": "not-for-display"},
    )

    assert allowed is None
    assert prompts == []


def _isolate_config(monkeypatch, root: Path) -> Path:
    settings_file = root / "settings.json"
    monkeypatch.setattr(config, "CONFIG_DIR", str(root))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(root / "api_keys.json"))
    with config._settings_cache_lock:
        config._settings_cache_signature = None
        config._settings_cache_data = {}
    return settings_file


def test_legacy_confirmation_settings_are_gone(monkeypatch, tmp_path):
    """WOR-301: the legacy confirmation keys and migration no longer exist."""

    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    config.save_settings({"unrelated": {"preserved": [1, 2, 3]}})

    effective = config.load_settings()
    assert "mcp_confirm_writes" not in effective
    assert "mcp_allow_unattended_writes" not in effective
    assert not hasattr(config, "migrate_legacy_confirmation_settings")


def _patch_gmail_draft_dispatch(monkeypatch, calls: list) -> None:
    from osia.tools import gmail

    monkeypatch.setattr(gmail, "_require_compose_permission", lambda: None)

    def fake_post(path, *, json_body, scopes, interactive=True):
        calls.append((path, json_body, scopes, interactive))
        return {"id": "draft-1", "message": {"id": "message-1"}}

    monkeypatch.setattr(gmail, "_gmail_post", fake_post)


def test_gmail_mutation_dispatches_unattended_without_confirmation(monkeypatch):
    """WOR-301: an unattended Gmail write executes under permanent Full Access.

    The removed confirmation boundary can no longer fail-closed an unattended
    mutation; execution-integrity (dispatch claim/attempt correlation) remains.
    """
    from osia.tools import gmail

    _legacy_unsafe_settings(monkeypatch)
    api_calls = []
    _patch_gmail_draft_dispatch(monkeypatch, api_calls)

    result = gmail.gmail_create_draft("person@example.test", "Subject", "Body")

    assert isinstance(result, dict), result
    assert result["status"] == "draft_created"
    assert result["changed"] is True
    assert len(api_calls) == 1
    assert api_calls[0][0] == "/drafts"
