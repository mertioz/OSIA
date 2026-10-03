from __future__ import annotations

import os
from pathlib import Path

import pytest

from osia import action_policy
from osia import deletion_plan as deletion_module
from osia import system_resources as system_module
from osia import agent as agent_module
from osia.mcp_client import registry as mcp_registry
from osia.system_resources import SystemResourceRegistry, WindowsRuntimeAdapter


def _safe_registry(tmp_path: Path) -> SystemResourceRegistry:
    unused = tmp_path / "never-system"
    return SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(unused),
            system_directory=str(unused / "System32"),
            system_windows_directory=str(unused),
            wrp_probe=lambda _path: False,
            storage_rows=(),
        ),
    )


def _protected_registry(root: Path) -> SystemResourceRegistry:
    return SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(root),
            system_directory=str(root),
            system_windows_directory=str(root),
            wrp_probe=lambda _path: True,
            storage_rows=(),
        ),
    )


@pytest.fixture(autouse=True)
def _policy_cleanup(monkeypatch, tmp_path):
    mcp_registry.set_confirmation_provider(None)
    safe = _safe_registry(tmp_path)
    monkeypatch.setattr(
        deletion_module,
        "get_default_system_resource_registry",
        lambda: safe,
    )
    monkeypatch.setattr(
        system_module,
        "get_default_system_resource_registry",
        lambda: safe,
    )
    yield
    mcp_registry.set_confirmation_provider(None)


def test_legacy_denial_provider_cannot_block_ordinary_deletion(tmp_path):
    """WOR-301: ordinary deletion proceeds with no confirmation provider."""
    target = tmp_path / "allowed.txt"
    target.write_text("delete", encoding="utf-8")
    mcp_registry.set_confirmation_provider(
        lambda *_args: pytest.fail("ordinary deletion must not prompt")
    )
    assert action_policy.guard_tool_call(
        "Delete the isolated fixture.", "delete_file", {"path": str(target)},
    ) is None
    assert agent_module.tool_delete_file(str(target)).startswith(
        "Deleted: SUCCESS_VERIFIED"
    )
    assert not target.exists()


def test_unbound_deletion_tool_has_zero_effect(tmp_path):
    """Anti-replay: without a bound plan the native tool deletes nothing."""
    target = tmp_path / "unbound.txt"
    target.write_text("stay", encoding="utf-8")
    assert agent_module.tool_delete_file(str(target)).startswith("ERROR:")
    assert target.exists()


def test_ordinary_deletion_deletes_exactly_once_without_confirmation(tmp_path):
    target = tmp_path / "approved.txt"
    target.write_text("delete", encoding="utf-8")
    prompts = []
    mcp_registry.set_confirmation_provider(
        lambda origin, tool, summary: prompts.append((origin, tool, summary)) or True
    )
    arguments = {"path": str(target)}
    assert action_policy.guard_tool_call(
        "Delete the isolated fixture.", "delete_file", arguments,
    ) is None
    result = agent_module.tool_delete_file(str(target))
    assert result.startswith("Deleted: SUCCESS_VERIFIED")
    assert not target.exists()
    assert agent_module.tool_delete_file(str(target)).startswith("ERROR:")
    assert prompts == []


def test_recursive_os_root_destruction_is_blocked_before_dispatch(tmp_path, monkeypatch):
    protected = tmp_path / "protected"
    protected.mkdir()
    target = protected / "system.bin"
    target.write_text("stay", encoding="utf-8")
    protected_registry = _protected_registry(protected)
    monkeypatch.setattr(
        deletion_module,
        "get_default_system_resource_registry",
        lambda: protected_registry,
    )
    monkeypatch.setattr(
        system_module,
        "get_default_system_resource_registry",
        lambda: protected_registry,
    )
    mcp_registry.set_confirmation_provider(
        lambda *_args: pytest.fail("system destruction must not prompt")
    )
    assert action_policy.guard_tool_call(
        "Delete the entire OS root.", "delete_directory",
        {"path": str(protected), "recursive": True},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert target.exists()


def test_single_file_operations_are_not_system_destruction_but_host_format_is(tmp_path, monkeypatch):
    protected = tmp_path / "protected-equivalents"
    protected.mkdir()
    target = protected / "system.cfg"
    target.write_text("required", encoding="utf-8")
    protected_registry = _protected_registry(protected)
    monkeypatch.setattr(
        system_module,
        "get_default_system_resource_registry",
        lambda: protected_registry,
    )
    assert action_policy.guard_tool_call(
        "Empty the target.",
        "write_file",
        {"path": str(target), "content": ""},
    ) is None
    assert agent_module.tool_write_file(str(target), "").startswith("Successfully wrote")
    assert agent_module.tool_write_file(str(target), "replacement").startswith("Successfully wrote")
    assert action_policy.guard_tool_call(
        "Move the target away.",
        "rename_file",
        {"old_path": str(target), "new_path": str(tmp_path / "moved")},
    ) is None
    assert action_policy.guard_tool_call(
        "Remove a partition.",
        "run_command",
        {"command": "Remove-Partition -DiskNumber 0 -PartitionNumber 1"},
    ) is None
    assert action_policy.guard_tool_call(
        "Format the mocked host volume.",
        "run_command",
        {"command": "format C:"},
    ) == "ERROR: BLOCK_SYSTEM_DESTRUCTION"
    assert target.read_text(encoding="utf-8") == "replacement"


def _colossal_tree(tmp_path: Path) -> Path:
    root = tmp_path / "colossal"
    root.mkdir()
    for index in range(999):
        (root / f"item-{index:04d}").write_bytes(b"x")
    return root


def test_colossal_deletion_proceeds_without_confirmation(tmp_path):
    """WOR-301: a large non-system deletion needs no confirmation permit.

    The system-destruction barrier is the only remaining authorization
    barrier, so the legacy reinforced-deletion provider must never be
    consulted for an ordinary colossal tree.
    """
    root = _colossal_tree(tmp_path)
    mcp_registry.set_confirmation_provider(
        lambda *_args: pytest.fail("colossal deletion must not prompt")
    )
    arguments = {"path": str(root), "recursive": True}
    assert action_policy.guard_tool_call(
        "Delete the synthetic tree.", "delete_directory", arguments,
    ) is None
    result = agent_module.tool_delete_directory(str(root), recursive=True)
    assert result.startswith("Recursively deleted directory: SUCCESS_VERIFIED")
    assert not root.exists()


def test_inventory_change_after_binding_invalidates_without_partial_effect(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    for name in ("a", "b", "c"):
        (root / name).write_text(name, encoding="utf-8")
    arguments = {"path": str(root), "recursive": True}
    assert action_policy.guard_tool_call(
        "Delete the isolated tree.", "delete_directory", arguments,
    ) is None
    added = root / "changed-after-binding"
    added.write_text("new", encoding="utf-8")
    result = agent_module.tool_delete_directory(str(root), recursive=True)
    assert result == "ERROR: DELETION_PLAN_CHANGED_REQUIRES_REVALIDATION"
    assert root.exists()
    assert len(tuple(root.iterdir())) == 4


def test_changed_inventory_tool_record_is_proven_before_effect(tmp_path):
    from osia.tool_contracts import ExecutionOutcome

    root = tmp_path / "tree"
    root.mkdir()
    for name in ("a", "b", "c"):
        (root / name).write_text(name, encoding="utf-8")
    arguments = {"path": str(root), "recursive": True}
    assert action_policy.guard_tool_call(
        "Delete the isolated tree.", "delete_directory", arguments,
    ) is None
    (root / "changed-after-binding").write_text("new", encoding="utf-8")
    _spec, contract = agent_module._tool_spec_and_contract(
        "delete_directory", arguments, "Delete the isolated tree.",
    )
    record = agent_module._execute_tool_once_record(
        "delete_directory",
        arguments,
        timeout_s=10.0,
        contract=contract,
        lang="en",
    )
    assert record.result == "ERROR: DELETION_PLAN_CHANGED_REQUIRES_REVALIDATION"
    assert record.outcome is ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert record.attempts == 0
    assert record.dispatched is False
    assert root.exists()
    assert len(tuple(root.iterdir())) == 4


def test_shell_output_cannot_spoof_proven_before_effect(monkeypatch):
    from osia.tool_contracts import ExecutionOutcome

    arguments = {"command": "printf synthetic"}
    _spec, contract = agent_module._tool_spec_and_contract(
        "bash", arguments, "Print synthetic output.",
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "bash",
        lambda **_kwargs: "ERROR: DELETION_PLAN_CHANGED_REQUIRES_REVALIDATION",
    )
    record = agent_module._execute_tool_once_record(
        "bash", arguments, timeout_s=10.0, contract=contract, lang="en",
    )
    assert record.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
    assert record.attempts == 1
    assert record.dispatched is True


def test_partial_known_effect_is_classified_and_never_replayed(tmp_path, monkeypatch):
    root = tmp_path / "partial"
    root.mkdir()
    for name in ("a", "b"):
        (root / name).write_text(name, encoding="utf-8")
    mcp_registry.set_confirmation_provider(lambda *_args: True)
    arguments = {"path": str(root), "recursive": True}
    assert action_policy.guard_tool_call(
        "Delete the isolated tree.", "delete_directory", arguments,
    ) is None
    original = os.unlink
    calls = []

    def fail_second(path):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("synthetic failure")
        return original(path)

    monkeypatch.setattr(deletion_module.os, "unlink", fail_second)
    result = agent_module.tool_delete_directory(str(root), recursive=True)
    assert "PARTIAL_EFFECT_VERIFIED" in result
    assert len(calls) == 2
    assert agent_module.tool_delete_directory(str(root), recursive=True).startswith("ERROR:")
    assert len(calls) == 2


def test_unverifiable_postcondition_is_outcome_unknown(tmp_path, monkeypatch):
    target = tmp_path / "unknown.txt"
    target.write_text("x", encoding="utf-8")
    mcp_registry.set_confirmation_provider(lambda *_args: True)
    assert action_policy.guard_tool_call(
        "Delete the fixture.", "delete_file", {"path": str(target)},
    ) is None
    monkeypatch.setattr(deletion_module, "_verify_affected", lambda _plan: (False, ()))
    result = agent_module.tool_delete_file(str(target))
    assert "OUTCOME_UNKNOWN" in result
    assert not target.exists()


def test_identity_swap_after_revalidation_is_not_deleted(tmp_path, monkeypatch):
    target = tmp_path / "identity-swap.txt"
    target.write_text("approved", encoding="utf-8")
    mcp_registry.set_confirmation_provider(lambda *_args: True)
    assert action_policy.guard_tool_call(
        "Delete the fixture.", "delete_file", {"path": str(target)},
    ) is None
    original_execute = deletion_module.execute_deletion_dispatch

    def replace_then_execute(dispatch):
        target.unlink()
        target.write_text("replacement", encoding="utf-8")
        return original_execute(dispatch)

    monkeypatch.setattr(
        deletion_module,
        "execute_deletion_dispatch",
        replace_then_execute,
    )
    result = agent_module.tool_delete_file(str(target))
    assert "CANCELLED_PROVEN" in result
    assert target.read_text(encoding="utf-8") == "replacement"


def test_model_supplied_classification_fields_are_rejected(tmp_path):
    target = tmp_path / "forgery.txt"
    target.write_text("stay", encoding="utf-8")
    blocked = action_policy.guard_tool_call(
        "Delete the fixture.",
        "delete_file",
        {"path": str(target), "colossal_deletion": False},
    )
    assert "untrusted_deletion_classification_field" in blocked
    assert target.exists()
