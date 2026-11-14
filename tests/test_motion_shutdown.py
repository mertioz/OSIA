"""Early GUI destruction must not raise from late animation callbacks."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QWidget

from osia.gui.motion import MotionController


APP = QApplication.instance() or QApplication([])


def test_parent_destruction_during_fade_has_no_late_qt_exception(monkeypatch):
    failures = []
    monkeypatch.setattr(sys, "excepthook", lambda *error: failures.append(error))
    root = QWidget()
    motion = MotionController(root, reduced_motion=lambda: False)
    child = QWidget(root)
    root.show()
    child.show()
    APP.processEvents()
    motion.fade_in(child, duration_ms=10000)
    assert motion.active_animation_count == 1
    root.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    APP.processEvents()
    assert failures == []
    assert motion.active_animation_count == 0


def test_settle_cleans_the_effect_even_if_its_animation_is_already_destroyed():
    root = QWidget()
    motion = MotionController(root, reduced_motion=lambda: False)
    child = QWidget(root)
    root.show()
    child.show()
    APP.processEvents()
    motion.fade_in(child, duration_ms=10000)
    animation = motion._states[id(child)][2]
    sip.delete(animation)
    try:
        motion.settle()
        assert motion.active_animation_count == 0
        assert child.graphicsEffect() is None
    finally:
        root.close()
        root.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
