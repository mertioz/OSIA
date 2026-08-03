"""Capture deterministic source/frozen visual evidence for WOR-225."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QEvent, QT_VERSION_STR, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from osia.gui.switch import OsiaSwitch
from osia.gui.theme import PALETTES, ThemeManager


def _manager(theme: str = "osia") -> ThemeManager:
    return ThemeManager(lambda: {"ui_theme": theme}, lambda *_args: None)


def _light_manager() -> ThemeManager:
    manager = _manager()
    manager._palette = replace(
        PALETTES["osia"],
        key="visual-light",
        background="#f7f5fb",
        surface="#ffffff",
        elevated_surface="#eeeaf5",
        glass="#f1edf8",
        primary="#6542c7",
        secondary="#8066c9",
        text="#17131f",
        muted_text="#625d6d",
        border="#bbb4c8",
        disabled="#77717f",
        user_bubble="#e9e2f8",
        assistant_bubble="#ffffff",
    )
    return manager


def _apply_gallery_style(widget: QWidget, manager: ThemeManager) -> None:
    palette = manager.palette
    widget.setStyleSheet(
        "QWidget#wor225Gallery {"
        f"background: {palette.background}; color: {palette.text};"
        "border-radius: 18px; }"
        "QFrame#wor225Card {"
        f"background: {palette.surface}; border: 1px solid {palette.border};"
        "border-radius: 12px; }"
        f"QLabel {{ color: {palette.text}; background: transparent; }}"
        f"QLabel#wor225Sub {{ color: {palette.muted_text}; }}"
    )


def _gallery(manager: ThemeManager, title: str) -> tuple[QWidget, dict[str, OsiaSwitch]]:
    gallery = QWidget()
    gallery.setObjectName("wor225Gallery")
    gallery.setFixedWidth(520)
    root = QVBoxLayout(gallery)
    root.setContentsMargins(24, 22, 24, 22)
    root.setSpacing(14)

    heading = QLabel(title)
    heading_font = heading.font()
    heading_font.setPointSize(16)
    heading_font.setBold(True)
    heading.setFont(heading_font)
    root.addWidget(heading)
    sub = QLabel("Canonical OSIA desktop switch · WOR-225")
    sub.setObjectName("wor225Sub")
    root.addWidget(sub)

    card = QFrame()
    card.setObjectName("wor225Card")
    rows = QVBoxLayout(card)
    rows.setContentsMargins(18, 14, 18, 14)
    rows.setSpacing(13)
    switches: dict[str, OsiaSwitch] = {}
    specs = (
        ("off", "OFF · neutral state", False, True),
        ("on", "ON · active accent", True, True),
        ("focus", "Keyboard focus", True, True),
        ("disabled", "Disabled", False, False),
        ("animation", "Animation midpoint", False, True),
    )
    for key, label_text, checked, enabled in specs:
        row = QHBoxLayout()
        label = QLabel(label_text)
        label.setMinimumWidth(240)
        switch = OsiaSwitch(
            theme_manager=manager,
            reduced_motion=lambda: False,
        )
        switch.setAccessibleName(label_text)
        switch.setChecked(checked)
        switch.setEnabled(enabled)
        row.addWidget(label, 1)
        row.addWidget(switch)
        rows.addLayout(row)
        switches[key] = switch
    root.addWidget(card)
    _apply_gallery_style(gallery, manager)
    gallery.adjustSize()
    return gallery, switches


def _save(widget: QWidget, path: Path) -> dict[str, object]:
    QApplication.processEvents()
    pixmap = widget.grab()
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Could not save {path}")
    return {
        "path": path.name,
        "logicalSize": [widget.width(), widget.height()],
        "pixelSize": [pixmap.width(), pixmap.height()],
        "devicePixelRatio": pixmap.devicePixelRatio(),
    }


def _capture_gallery(output: Path, prefix: str) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    manager = _manager()
    gallery, switches = _gallery(manager, "OSIA Violet · dark")
    gallery.show()
    switches["focus"].setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    evidence.append(_save(gallery, output / f"{prefix}-dark-states.png"))

    QApplication.sendEvent(switches["on"], QEvent(QEvent.Type.Enter))
    QApplication.processEvents()
    evidence.append(_save(gallery, output / f"{prefix}-hover-state.png"))
    QTest.mousePress(
        switches["on"],
        Qt.MouseButton.LeftButton,
        pos=switches["on"].rect().center(),
    )
    QApplication.processEvents()
    evidence.append(_save(gallery, output / f"{prefix}-pressed-state.png"))
    QTest.mouseRelease(
        switches["on"],
        Qt.MouseButton.LeftButton,
        pos=switches["on"].rect().center(),
    )
    QApplication.sendEvent(switches["on"], QEvent(QEvent.Type.Leave))
    switches["on"].setChecked(True)
    QApplication.processEvents()

    switches["animation"].setChecked(True)
    QTest.qWait(78)
    QApplication.processEvents()
    midpoint = switches["animation"].thumb_position
    if not 0.0 < midpoint < 1.0:
        raise AssertionError(f"animation midpoint is not intermediate: {midpoint}")
    item = _save(gallery, output / f"{prefix}-animation-midpoint.png")
    item["thumbPosition"] = midpoint
    evidence.append(item)

    manager.set_theme("azure", persist=False)
    _apply_gallery_style(gallery, manager)
    switches["animation"].setChecked(False)
    QTest.qWait(OsiaSwitch.ANIMATION_MS + 30)
    QApplication.processEvents()
    evidence.append(_save(gallery, output / f"{prefix}-azure-states.png"))
    gallery.close()
    gallery.deleteLater()

    light_manager = _light_manager()
    light_gallery, light_switches = _gallery(light_manager, "OSIA · light")
    light_gallery.show()
    light_switches["focus"].setFocus(Qt.FocusReason.TabFocusReason)
    QApplication.processEvents()
    evidence.append(_save(light_gallery, output / f"{prefix}-light-states.png"))
    light_gallery.close()
    light_gallery.deleteLater()
    return evidence


def _capture_settings(output: Path, prefix: str) -> list[dict[str, object]]:
    main_popup = importlib.import_module("osia.gui.main_popup")
    popup = main_popup.MainPopup()
    popup.show()
    QApplication.processEvents()
    evidence: list[dict[str, object]] = []
    for category in (
        "settings.cat.general",
        "settings.cat.permissions",
        "settings.cat.models",
        "settings.cat.voice",
        "settings.cat.wake",
        "settings.cat.advanced",
    ):
        popup.show_settings_category(category)
        QTest.qWait(260)
        QApplication.processEvents()
        slug = category.rsplit(".", 1)[-1]
        evidence.append(_save(popup, output / f"{prefix}-settings-{slug}.png"))
    popup.close()
    popup.deleteLater()
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("OSIA WOR-225 visual smoke")
    prefix = "frozen" if getattr(sys, "frozen", False) else "source"
    evidence = _capture_gallery(args.output, prefix)
    if args.settings:
        evidence.extend(_capture_settings(args.output, prefix))
    manifest = {
        "mode": prefix,
        "qtVersion": QT_VERSION_STR,
        "files": evidence,
    }
    (args.output / f"{prefix}-manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
