from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

import osia.deletion_plan as deletion_module

from osia.deletion_plan import (
    ColossalDeletionPolicy,
    DeletionPermitLedger,
    DeletionPlan,
    DeletionPlanError,
    InventoryEntryType,
    RevalidationState,
    build_deletion_plan,
    revalidate_deletion_plan,
)
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


def test_one_file_and_one_empty_directory_have_complete_exact_inventory(tmp_path):
    registry = _safe_registry(tmp_path)
    file = tmp_path / "one.txt"
    file.write_bytes(b"abc")
    folder = tmp_path / "empty"
    folder.mkdir()
    file_plan = build_deletion_plan([file], recursive=False, registry=registry)
    folder_plan = build_deletion_plan([folder], recursive=False, registry=registry)
    assert (file_plan.files_count, file_plan.folders_count, file_plan.total_bytes) == (1, 0, 3)
    assert (folder_plan.files_count, folder_plan.folders_count) == (0, 1)
    assert file_plan.entries[0].entry_type is InventoryEntryType.FILE
    assert folder_plan.entries[0].entry_type is InventoryEntryType.FOLDER


def test_recursive_tree_is_ordered_complete_and_not_truncated(tmp_path):
    root = tmp_path / "tree"
    (root / "b" / "nested").mkdir(parents=True)
    (root / "a").mkdir()
    (root / "b" / "nested" / "z.txt").write_text("z", encoding="utf-8")
    (root / "a" / "x.txt").write_text("xx", encoding="utf-8")
    plan = build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))
    names = [item.relative_path.replace("\\", "/") for item in plan.entries]
    assert names == sorted(names)
    assert plan.files_count == 2
    assert plan.folders_count == 4
    assert len(plan.entries) == 6
    assert len(plan._local_entries()) == 6


def test_non_recursive_non_empty_directory_is_rejected(tmp_path):
    root = tmp_path / "not-empty"
    root.mkdir()
    (root / "child").write_text("x", encoding="utf-8")
    with pytest.raises(DeletionPlanError, match="non_recursive_directory_not_empty"):
        build_deletion_plan([root], recursive=False, registry=_safe_registry(tmp_path))


def test_multiple_roots_are_deterministic_and_three_roots_are_colossal(tmp_path):
    roots = []
    for name in ("z.txt", "a.txt", "m.txt"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        roots.append(path)
    plan = build_deletion_plan(roots, recursive=False, registry=_safe_registry(tmp_path))
    assert plan.root_count == 3
    assert plan.colossal_deletion is True
    assert "hard_root_threshold" in plan.reason_codes
    assert list(plan._local_roots()) == sorted(plan._local_roots(), key=os.path.normcase)


def test_symlink_is_a_leaf_and_target_is_not_inventoried(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "private.txt").write_text("stay", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    plan = build_deletion_plan([link], recursive=True, registry=_safe_registry(tmp_path))
    assert plan.link_reparse_count == 1
    assert len(plan.entries) == 1
    assert plan.entries[0].entry_type in {
        InventoryEntryType.SYMLINK,
        InventoryEntryType.REPARSE_POINT,
    }


def test_hash_is_stable_and_changes_for_add_delete_and_size_change(tmp_path):
    root = tmp_path / "hash-tree"
    root.mkdir()
    first = root / "first.txt"
    first.write_text("a", encoding="utf-8")
    registry = _safe_registry(tmp_path)
    one = build_deletion_plan([root], recursive=True, registry=registry)
    two = build_deletion_plan([root], recursive=True, registry=registry)
    assert one.inventory_hash == two.inventory_hash
    second = root / "second.txt"
    second.write_text("b", encoding="utf-8")
    added = build_deletion_plan([root], recursive=True, registry=registry)
    assert added.inventory_hash != one.inventory_hash
    second.unlink()
    deleted = build_deletion_plan([root], recursive=True, registry=registry)
    assert deleted.inventory_hash != added.inventory_hash
    first.write_text("different-size", encoding="utf-8")
    resized = build_deletion_plan([root], recursive=True, registry=registry)
    assert resized.inventory_hash != one.inventory_hash


def test_revalidation_rejects_any_inventory_change(tmp_path):
    root = tmp_path / "change"
    root.mkdir()
    (root / "one").write_text("1", encoding="utf-8")
    registry = _safe_registry(tmp_path)
    plan = build_deletion_plan([root], recursive=True, registry=registry)
    validated = revalidate_deletion_plan(plan, registry=registry)
    assert plan.revalidation_state is RevalidationState.PENDING
    assert validated.revalidation_state is RevalidationState.VALIDATED
    (root / "two").write_text("2", encoding="utf-8")
    with pytest.raises(DeletionPlanError, match="deletion_plan_changed"):
        revalidate_deletion_plan(plan, registry=registry)


def test_injectable_thresholds_classify_colossal_without_reinforcement(tmp_path):
    file = tmp_path / "threshold.txt"
    file.write_text("x", encoding="utf-8")
    policy = ColossalDeletionPolicy(
        object_hard_limit=1,
        byte_hard_limit=10_000,
        root_hard_limit=10,
        object_risk_limit=10,
        byte_risk_limit=10_000,
        risk_score_limit=10,
    )
    plan = build_deletion_plan(
        [file], recursive=False, registry=_safe_registry(tmp_path), policy=policy,
    )
    assert plan.colossal_deletion is True
    # WOR-301: a colossal non-system deletion needs no reinforced user
    # confirmation; the exact bound plan is the integrity token.
    ledger = DeletionPermitLedger()
    permit = ledger.issue(
        plan, policy_epoch=0, confirmation_strength="ordinary",
    )
    assert permit.used is False
    assert ledger.consume(permit, plan, policy_epoch=0).used is True


def test_risk_score_threshold_marks_sensitive_recursive_repository(tmp_path):
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    (root / "file.txt").write_text("x", encoding="utf-8")
    plan = build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))
    assert plan.risk_score >= 3
    assert plan.colossal_deletion is True


def test_safe_summary_and_repr_never_contain_private_path(tmp_path):
    private = tmp_path / "private-name.txt"
    private.write_text("x", encoding="utf-8")
    plan = build_deletion_plan([private], recursive=False, registry=_safe_registry(tmp_path))
    assert str(private) not in repr(plan)
    assert str(private) not in str(plan.safe_public_summary())
    assert "inventory_hash" not in plan.safe_public_summary()


def test_plan_and_inventory_cannot_be_fabricated(tmp_path):
    file = tmp_path / "real.txt"
    file.write_text("x", encoding="utf-8")
    real = build_deletion_plan([file], recursive=False, registry=_safe_registry(tmp_path))
    with pytest.raises(ValueError, match="untrusted"):
        replace(real, _seal=object())


def test_access_denied_and_disappearing_root_are_not_silently_omitted(tmp_path, monkeypatch):
    root = tmp_path / "blocked"
    root.mkdir()
    original_scandir = os.scandir

    def denied(path):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(root)):
            raise PermissionError("fixture")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(DeletionPlanError, match="inventory_access_denied"):
        build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))
    monkeypatch.setattr(os, "scandir", original_scandir)
    root.rmdir()
    with pytest.raises(DeletionPlanError, match="root_missing"):
        build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))


def test_mount_boundary_is_a_leaf_and_never_traversed(tmp_path, monkeypatch):
    root = tmp_path / "mount-boundary"
    mounted = root / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "must-not-enter.txt").write_text("fixture", encoding="utf-8")
    original_ismount = os.path.ismount
    monkeypatch.setattr(
        os.path,
        "ismount",
        lambda path: os.path.normcase(os.fspath(path)) == os.path.normcase(str(mounted))
        or original_ismount(path),
    )
    plan = build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))
    assert [item.entry_type for item in plan.entries] == [
        InventoryEntryType.FOLDER,
        InventoryEntryType.MOUNT_POINT,
    ]
    assert all("must-not-enter" not in item.relative_path for item in plan.entries)


def test_reparse_directory_fixture_is_a_non_traversed_leaf(tmp_path, monkeypatch):
    root = tmp_path / "reparse-boundary"
    link = root / "junction"
    link.mkdir(parents=True)
    (link / "must-not-enter.txt").write_text("fixture", encoding="utf-8")
    original_entry_type = deletion_module._entry_type

    def fixture_entry_type(path, info):
        if os.path.normcase(os.fspath(path)) == os.path.normcase(str(link)):
            return InventoryEntryType.REPARSE_POINT, "reparse_dir"
        return original_entry_type(path, info)

    monkeypatch.setattr(deletion_module, "_entry_type", fixture_entry_type)
    plan = build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))
    assert plan.link_reparse_count == 1
    assert len(plan.entries) == 2
    assert all("must-not-enter" not in item.relative_path for item in plan.entries)


def test_path_escape_and_repeated_directory_identity_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / "loop-root"
    child = root / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("fixture", encoding="utf-8")
    with pytest.raises(DeletionPlanError, match="inventory_path_escape"):
        deletion_module._entry(0, str(root), str(outside), os.lstat(outside))

    original_entry = deletion_module._entry
    root_identity = {}

    def repeated_identity(root_index, root_path, path, info):
        item = original_entry(root_index, root_path, path, info)
        if item.relative_path == ".":
            root_identity["value"] = (item.device_id, item.object_id)
            return item
        if item.entry_type is InventoryEntryType.FOLDER:
            return replace(
                item,
                device_id=root_identity["value"][0],
                object_id=root_identity["value"][1],
            )
        return item

    monkeypatch.setattr(deletion_module, "_entry", repeated_identity)
    with pytest.raises(DeletionPlanError, match="inventory_traversal_loop"):
        build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))


def test_policy_has_no_maximum_inventory_limit(tmp_path):
    root = tmp_path / "over-default-threshold"
    root.mkdir()
    for index in range(1001):
        (root / f"entry-{index:04d}").write_bytes(b"")
    plan = build_deletion_plan([root], recursive=True, registry=_safe_registry(tmp_path))
    assert len(plan.entries) == 1002
    assert plan.colossal_deletion is True
