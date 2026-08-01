from __future__ import annotations

import json
from pathlib import Path

import pytest

from osia import action_policy
from osia import agent as agent_module
from osia import config
from osia.mcp_client import registry
from osia.tools import gmail


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


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("delete_file", {"path": "opaque-file"}),
        ("delete_directory", {"path": "opaque-directory", "recursive": True}),
        ("run_command", {"command": "echo private-value"}),
    ],
)
def test_native_sensitive_actions_ignore_legacy_mcp_values_and_require_approval(
    monkeypatch, tool_name, arguments,
):
    _legacy_unsafe_settings(monkeypatch)
    prompts = []
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or False
    )

    error = action_policy.guard_tool_call(
        "Perform the requested local operation.", tool_name, arguments,
    )

    assert error.startswith("ERROR: action/osia/")
    assert len(prompts) == 1
    assert prompts[0][0:2] == ("osia", tool_name)
    assert all(str(value) not in prompts[0][2] for value in arguments.values())


@pytest.mark.parametrize("provider_result", [False, None])
def test_denial_or_confirmation_timeout_prevents_native_dispatch(
    monkeypatch, provider_result,
):
    _legacy_unsafe_settings(monkeypatch)
    dispatches = []
    registry.set_confirmation_provider(lambda *_args: provider_result)

    blocked = action_policy.guard_tool_call(
        "Delete the requested local file.",
        "delete_file",
        {"path": "opaque-file"},
    )
    if blocked is None:
        dispatches.append("unexpected")

    assert blocked.startswith("ERROR:")
    assert dispatches == []


@pytest.mark.parametrize("label", ["Send", "Submit", "Buy", "Pay"])
def test_browser_commit_labels_still_require_confirmation(monkeypatch, label):
    _legacy_unsafe_settings(monkeypatch)
    prompts = []
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or False
    )
    request = {
        "Send": 'Send exactly one message to Contact Alpha: "Hello".',
        "Submit": "Submit this completed external form.",
        "Buy": "Buy the selected item.",
        "Pay": "Pay the displayed invoice.",
    }[label]

    error = action_policy.guard_tool_call(
        request, "find_and_click", {"text": label},
    )

    assert error.startswith("ERROR: action/osia/")
    assert len(prompts) == 1


def test_structural_browser_commit_is_conservative_when_request_is_unrecognized(
    monkeypatch,
):
    _legacy_unsafe_settings(monkeypatch)
    prompts = []
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or False
    )

    error = action_policy.guard_tool_call(
        "Complete the external operation.", "press_key", {"key": "Enter"},
    )

    assert error.startswith("ERROR: action/osia/")
    assert len(prompts) == 1


def _patch_gmail_draft_dispatch(monkeypatch, calls: list) -> None:
    monkeypatch.setattr(gmail, "_require_compose_permission", lambda: None)

    def fake_post(path, *, json_body, scopes, interactive=True):
        calls.append((path, json_body, scopes, interactive))
        return {"id": "draft-1", "message": {"id": "message-1"}}

    monkeypatch.setattr(gmail, "_gmail_post", fake_post)


def test_gmail_mutation_cannot_be_auto_approved_by_legacy_confirm_value(monkeypatch):
    _legacy_unsafe_settings(monkeypatch)
    prompts = []
    api_calls = []
    _patch_gmail_draft_dispatch(monkeypatch, api_calls)
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or True
    )

    result = gmail.gmail_create_draft("person@example.test", "Subject", "Body")

    assert result["changed"] is True
    assert len(prompts) == 1
    assert prompts[0][0:2] == ("gmail", "gmail_create_draft")
    assert len(api_calls) == 1


def test_gmail_mutation_fails_closed_unattended_despite_legacy_allow(monkeypatch):
    _legacy_unsafe_settings(monkeypatch)
    api_calls = []
    _patch_gmail_draft_dispatch(monkeypatch, api_calls)

    result = gmail.gmail_create_draft("person@example.test", "Subject", "Body")

    assert isinstance(result, str) and result.startswith("ERROR:")
    assert api_calls == []


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


@pytest.mark.parametrize("context_name", ["pc_interactive", "mobile_interactive"])
def test_mcp_write_requires_exact_interactive_confirmation(
    monkeypatch, context_name,
):
    _legacy_unsafe_settings(monkeypatch)
    manager = _Manager()
    prompts = []
    registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append(
            (context_name, origin, tool, summary)
        ) or True
    )

    assert _mcp_wrapper(manager, is_write=True)(item="fixture") == "ok"
    assert len(prompts) == 1
    assert len(manager.calls) == 1


@pytest.mark.parametrize(
    ("context_name", "provider"),
    [
        ("autonomous_headless", None),
        ("scheduled_task_headless", lambda *_args: None),
    ],
)
def test_mcp_write_fails_closed_in_headless_contexts(
    monkeypatch, context_name, provider,
):
    del context_name
    _legacy_unsafe_settings(monkeypatch)
    manager = _Manager()
    registry.set_confirmation_provider(provider)

    result = _mcp_wrapper(manager, is_write=True)(item="fixture")

    assert result.startswith("ERROR: mcp/")
    assert "no approver available" in result
    assert manager.calls == []


@pytest.mark.parametrize(
    "domain",
    [
        registry.ConfirmationDomain.NATIVE,
        registry.ConfirmationDomain.GMAIL,
        registry.ConfirmationDomain.MCP,
    ],
)
def test_every_sensitive_domain_fails_closed_without_provider(monkeypatch, domain):
    _legacy_unsafe_settings(monkeypatch)
    dispatches = []

    blocked = registry.confirm_sensitive_action(
        domain,
        "fixture-origin",
        "fixture_tool",
        "Perform one sensitive action",
        call_arguments={"private": "not-for-display"},
    )
    if blocked is None:
        dispatches.append("unexpected")

    assert blocked.startswith("ERROR:")
    assert dispatches == []
    assert "not-for-display" not in blocked


def _isolate_config(monkeypatch, root: Path) -> Path:
    settings_file = root / "settings.json"
    monkeypatch.setattr(config, "CONFIG_DIR", str(root))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(config, "API_KEYS_FILE", str(root / "api_keys.json"))
    with config._settings_cache_lock:
        config._settings_cache_signature = None
        config._settings_cache_data = {}
    return settings_file


def test_legacy_settings_migrate_once_without_rewriting_unrelated_values(
    monkeypatch, tmp_path,
):
    settings_file = _isolate_config(monkeypatch, tmp_path)
    original = {
        "mcp_confirm_writes": False,
        "mcp_allow_unattended_writes": True,
        "unrelated": {"preserved": [1, 2, 3]},
    }
    settings_file.write_text(json.dumps(original), encoding="utf-8")

    effective = config.load_settings()
    assert effective["mcp_confirm_writes"] is True
    assert effective["mcp_allow_unattended_writes"] is False
    assert config.migrate_legacy_confirmation_settings() is True
    migrated = json.loads(settings_file.read_text(encoding="utf-8"))
    assert migrated["mcp_confirm_writes"] is True
    assert migrated["mcp_allow_unattended_writes"] is False
    assert migrated["unrelated"] == original["unrelated"]
    first_bytes = settings_file.read_bytes()
    assert config.migrate_legacy_confirmation_settings() is False
    assert settings_file.read_bytes() == first_bytes


def test_legacy_setting_writes_and_mobile_snapshot_remain_safe(monkeypatch, tmp_path):
    settings_file = _isolate_config(monkeypatch, tmp_path)
    settings_file.write_text("{}", encoding="utf-8")

    config.set_setting("mcp_confirm_writes", False)
    config.set_setting("mcp_allow_unattended_writes", True)
    persisted = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted["mcp_confirm_writes"] is True
    assert persisted["mcp_allow_unattended_writes"] is False

    from osia import personality
    from osia.server import server

    monkeypatch.setattr(server, "_gmail_rest_config_snapshot", lambda: {})
    monkeypatch.setattr(
        personality,
        "resolve_personality",
        lambda: personality.PersonalitySelection(
            "default", "", "test", "missing", ""
        ),
    )
    monkeypatch.setattr(personality, "discover_skills", lambda: [])
    snapshot = server._build_config_snapshot()
    assert snapshot["mcp_confirm_writes"] is True
    assert snapshot["mcp_allow_unattended_writes"] is False


def test_disposable_deletion_denied_approved_once_and_unattended(
    monkeypatch, tmp_path,
):
    _legacy_unsafe_settings(monkeypatch)
    denied = tmp_path / "denied.txt"
    approved = tmp_path / "approved.txt"
    unattended = tmp_path / "unattended.txt"
    for path in (denied, approved, unattended):
        path.write_text("synthetic WOR-191 fixture", encoding="utf-8")
    dispatches = []

    def attempt(path: Path, provider):
        registry.set_confirmation_provider(provider)
        blocked = action_policy.guard_tool_call(
            "Delete the isolated temporary fixture.",
            "delete_file",
            {"path": str(path)},
        )
        if blocked is not None:
            return blocked
        dispatches.append(path.name)
        return agent_module.tool_delete_file(str(path))

    assert attempt(denied, lambda *_args: False).startswith("ERROR:")
    assert denied.exists()
    assert not attempt(approved, lambda *_args: True).startswith("ERROR:")
    assert not approved.exists()
    assert attempt(unattended, None).startswith("ERROR:")
    assert unattended.exists()
    assert dispatches == ["approved.txt"]
