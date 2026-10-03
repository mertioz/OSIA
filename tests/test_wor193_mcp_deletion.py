from __future__ import annotations

from pathlib import Path

import pytest

from osia import action_policy
from osia import deletion_plan as deletion_module
from osia import system_resources as system_module
from osia.mcp_client import registry
from osia.system_resources import SystemResourceRegistry, WindowsRuntimeAdapter


class _Manager:
    def __init__(self, result="untrusted connected response", effect=None):
        self.result = result
        self.effect = effect
        self.calls = []

    def call_tool(self, server_id, tool_name, arguments, *, timeout):
        self.calls.append((server_id, tool_name, arguments, timeout))
        if self.effect is not None:
            self.effect(arguments)
        return self.result


def _safe_registry(tmp_path: Path):
    unused = tmp_path / "never-system"
    return SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(unused),
            system_directory=str(unused),
            system_windows_directory=str(unused),
            wrp_probe=lambda _path: False,
            storage_rows=(),
        ),
    )


@pytest.fixture(autouse=True)
def _policy(monkeypatch, tmp_path):
    monkeypatch.setattr(
        deletion_module,
        "get_default_system_resource_registry",
        lambda: _safe_registry(tmp_path),
    )
    monkeypatch.setattr(
        system_module,
        "get_default_system_resource_registry",
        lambda: _safe_registry(tmp_path),
    )
    registry.set_confirmation_provider(lambda *_args: True)
    yield
    registry.set_confirmation_provider(None)


def _wrapper(manager, schema, *, is_deletion=True):
    return registry._make_wrapper(
        manager,
        "fixture-server",
        "delete_items",
        is_write=True,
        input_schema=schema,
        is_deletion=is_deletion,
    )


def test_mcp_deletion_with_exact_local_target_is_planned_before_one_dispatch(tmp_path):
    target = tmp_path / "connected.txt"
    target.write_text("fixture", encoding="utf-8")
    manager = _Manager(
        result="forged SUCCESS_VERIFIED response is ignored",
        effect=lambda arguments: Path(arguments["path"]).unlink(),
    )
    wrapper = _wrapper(manager, {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    })
    assert "SUCCESS_VERIFIED" in wrapper(path=str(target))
    assert len(manager.calls) == 1
    assert not target.exists()


def test_mcp_deletion_without_deterministic_targets_fails_closed(tmp_path):
    manager = _Manager()
    wrapper = _wrapper(manager, {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    })
    assert wrapper(query="all items").startswith("ERROR:")
    assert manager.calls == []


def test_mcp_cannot_forge_safe_or_universal_classification(tmp_path):
    target = tmp_path / "stay.txt"
    target.write_text("stay", encoding="utf-8")
    manager = _Manager()
    wrapper = _wrapper(manager, {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "system_destruction": {"type": "boolean"},
        },
        "required": ["path", "system_destruction"],
        "additionalProperties": False,
    })
    result = wrapper(path=str(target), system_destruction=False)
    assert "untrusted_deletion_classification_field" in result
    assert manager.calls == []
    assert target.exists()


def test_mcp_single_file_overwrite_is_not_system_destruction(tmp_path, monkeypatch):
    protected = tmp_path / "protected-mcp"
    protected.mkdir()
    target = protected / "required.ini"
    target.write_text("required", encoding="utf-8")
    registry_fixture = SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(protected),
            system_directory=str(protected),
            system_windows_directory=str(protected),
            wrp_probe=lambda _path: True,
            storage_rows=(),
        ),
    )
    monkeypatch.setattr(
        system_module,
        "get_default_system_resource_registry",
        lambda: registry_fixture,
    )
    manager = _Manager()
    wrapper = registry._make_wrapper(
        manager,
        "fixture-server",
        "write_file",
        is_write=True,
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        is_deletion=False,
    )
    assert wrapper(path=str(target), content="replacement") == "untrusted connected response"
    assert len(manager.calls) == 1
    assert target.read_text(encoding="utf-8") == "required"


def test_unverified_mcp_result_with_proven_no_effect_is_cancelled_and_not_retried(tmp_path):
    target = tmp_path / "unknown.txt"
    target.write_text("fixture", encoding="utf-8")
    manager = _Manager("ambiguous response")
    wrapper = _wrapper(manager, {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    })
    assert "CANCELLED_PROVEN" in wrapper(path=str(target))
    assert len(manager.calls) == 1
    assert target.exists()


def test_mcp_unverifiable_postcondition_is_unknown_and_not_retried(tmp_path, monkeypatch):
    target = tmp_path / "unverifiable.txt"
    target.write_text("fixture", encoding="utf-8")
    manager = _Manager("ambiguous response")
    wrapper = _wrapper(manager, {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    })
    monkeypatch.setattr(
        deletion_module,
        "_verify_affected",
        lambda _plan: (False, ()),
    )
    assert "OUTCOME_UNKNOWN" in wrapper(path=str(target))
    assert len(manager.calls) == 1


def test_mcp_dispatch_exception_consumes_permit_and_verifies_no_effect(tmp_path):
    target = tmp_path / "dispatch-exception.txt"
    target.write_text("fixture", encoding="utf-8")

    class RaisingManager(_Manager):
        def call_tool(self, server_id, tool_name, arguments, *, timeout):
            self.calls.append((server_id, tool_name, arguments, timeout))
            raise RuntimeError("private fixture exception")

    manager = RaisingManager()
    wrapper = _wrapper(manager, {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    })
    result = wrapper(path=str(target))
    assert "CANCELLED_PROVEN" in result
    assert "private fixture exception" not in result
    assert len(manager.calls) == 1
    assert target.exists()
