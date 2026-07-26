from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

from osia.deletion_plan import ColossalDeletionPolicy, build_deletion_plan
from osia.gui.deletion_confirm import (
    DeletionConfirmController,
    DeletionInventoryModel,
    ReinforcedDeletionDialog,
)
from osia.system_resources import SystemResourceRegistry, WindowsRuntimeAdapter


_APP = QApplication.instance() or QApplication([])


def _plan(tmp_path: Path, count: int = 25):
    root = tmp_path / "private-inventory-root"
    root.mkdir(parents=True)
    for index in range(count):
        (root / f"entry-{index:03d}.txt").write_text("x", encoding="utf-8")
    unused = tmp_path / "never-system"
    registry = SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(unused),
            system_directory=str(unused),
            system_windows_directory=str(unused),
            wrp_probe=lambda _path: False,
            storage_rows=(),
        ),
    )
    policy = ColossalDeletionPolicy(
        object_hard_limit=1,
        byte_hard_limit=1_000_000,
        root_hard_limit=10,
        object_risk_limit=1_000,
        byte_risk_limit=1_000_000,
        risk_score_limit=10,
    )
    return build_deletion_plan(
        [root], recursive=True, registry=registry, policy=policy,
    )


def test_inventory_model_keeps_every_entry_and_private_paths_local(tmp_path):
    plan = _plan(tmp_path, 50)
    model = DeletionInventoryModel(plan)
    assert model.total_entries == len(plan.entries) == 51
    assert model.rowCount() == 1
    root_index = model.index(0, 0)
    assert model.rowCount(root_index) == 50
    assert str(tmp_path) in str(model.data(root_index))
    assert str(tmp_path) not in str(plan.safe_public_summary())


def test_enter_close_and_escape_cannot_approve_but_allow_click_can(tmp_path):
    dialog = ReinforcedDeletionDialog(None, _plan(tmp_path, 1))
    dialog.show()
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    assert dialog.result() != QDialog.DialogCode.Accepted
    dialog._allow_button.setFocus()
    QTest.keyClick(dialog._allow_button, Qt.Key.Key_Space)
    assert dialog.result() != QDialog.DialogCode.Accepted
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == QDialog.DialogCode.Rejected

    closed = ReinforcedDeletionDialog(None, _plan(tmp_path / "closed", 1))
    closed.show()
    closed.close()
    assert closed.result() != QDialog.DialogCode.Accepted

    approved = ReinforcedDeletionDialog(None, _plan(tmp_path / "second", 1))
    approved.show()
    QTest.mouseClick(approved._allow_button, Qt.MouseButton.LeftButton)
    assert approved.result() == QDialog.DialogCode.Accepted
    assert approved._allow_clicked is True


def test_expired_plan_never_reopens_a_local_dialog(tmp_path, monkeypatch):
    plan = _plan(tmp_path, 1)
    expired = replace(
        plan,
        created_at=time.time() - 60,
        expires_at=time.time() - 30,
    )
    controller = DeletionConfirmController(None)
    monkeypatch.setattr(
        controller,
        "_run_modal",
        lambda _plan: pytest.fail("expired review must not open"),
    )
    assert controller.confirm(expired) is False
