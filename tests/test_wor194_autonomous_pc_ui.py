"""Focused offscreen checks for the PC Autonomous authorization panel."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication, QScrollArea

from osia.gui.main_popup import _AutonomousPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    value = _AutonomousPanel()
    try:
        yield value
    finally:
        value.deleteLater()


def test_plan_review_requires_explicit_authorize_and_start(panel):
    emitted = []
    panel.authorize_requested.connect(lambda: emitted.append("authorize"))
    panel.apply_progress({
        "event": "autonomous_plan_ready",
        "run_state": "authorization_pending",
        "plan_summary": [{"name": "Prepare", "tasks": ["Write a report"]}],
        "capability_summary": [{
            "capability_id": "cap_one",
            "label": "Write bounded local text",
            "effect_class": "persistent_local",
            "target_precision": "exact",
        }],
        "private_scope_details": {"cap_one": r"C:\Temp\report.txt"},
        "authorization_source": "none",
        "grant_refs": [],
    })
    assert not panel._authorize_btn.isHidden()
    assert panel._authorize_btn.isEnabled()
    assert not panel._interrupt_btn.isEnabled()
    assert "Prepare" in panel._plan_review.text()
    assert "Write bounded local text" in panel._capability_review.text()
    assert "report.txt" in panel._capability_review.text()
    panel._authorize_btn.click()
    assert emitted == ["authorize"]


def test_long_plan_keeps_authorize_button_reachable(panel, app):
    panel.resize(440, 560)
    panel.show()
    panel.apply_progress({
        "event": "autonomous_plan_ready",
        "run_state": "authorization_pending",
        "plan_summary": [{
            "name": "Prepare",
            "tasks": ["Synthetic task detail " * 12 for _ in range(4)],
        }],
        "capability_summary": [{
            "capability_id": "cap_one",
            "label": "Write bounded local text",
            "effect_class": "persistent_local",
            "target_precision": "exact",
        }],
        "private_scope_details": {"cap_one": r"C:\Temp\synthetic.txt"},
    })
    app.processEvents()
    scroll = panel.findChild(QScrollArea, "panelScroll")
    bar = scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    app.processEvents()
    top = panel._authorize_btn.mapTo(scroll.viewport(), QPoint(0, 0)).y()
    bottom = top + panel._authorize_btn.height()
    assert top >= 0
    assert bottom <= scroll.viewport().height()


def test_scope_extension_shows_pc_detail_and_universal_barrier(panel):
    panel.apply_progress({
        "event": "suspended_scope_extension",
        "pending_scope_extension": {
            "safe_category": "local_path",
            "effect_class": "persistent_local",
            "requested_use_count": 1,
            "local_pc_review_required": True,
            "can_approve_pc": True,
            "private_scope_detail": r"C:\Temp\second.txt",
            "universal_barrier": "",
        },
    })
    assert not panel._scope_frame.isHidden()
    assert panel._scope_approve_btn.isEnabled()
    assert "second.txt" in panel._scope_detail.text()

    panel.apply_progress({
        "event": "suspended_scope_extension",
        "pending_scope_extension": {
            "safe_category": "local_path",
            "effect_class": "destructive_local",
            "requested_use_count": 1,
            "local_pc_review_required": True,
            "universal_barrier": "system_destruction",
        },
    })
    assert not panel._scope_approve_btn.isEnabled()

    panel.apply_progress({
        "event": "suspended_scope_extension",
        "pending_scope_extension": {
            "safe_category": "local_command",
            "effect_class": "unknown_high_risk",
            "can_approve_pc": False,
            "universal_barrier": "",
        },
    })
    assert not panel._scope_approve_btn.isEnabled()


@pytest.mark.parametrize(
    "event",
    ["suspended_authorization_revoked", "blocked_outcome_unknown"],
)
def test_terminal_authorization_barriers_have_no_approve_action(panel, event):
    panel.apply_progress({"event": event})
    assert panel._authorize_btn.isHidden()
    assert panel._scope_frame.isHidden()
    assert panel._interrupt_btn.isEnabled() is False
    assert panel._prog_status.text()
