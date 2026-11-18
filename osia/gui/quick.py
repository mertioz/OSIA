"""Load local Qt Quick surfaces for the desktop UI."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtQuickWidgets import QQuickWidget


def load_quick_view(
    view: QQuickWidget,
    filename: str,
    properties: dict | None = None,
) -> None:
    """Load a synchronous, transparent application-owned QML component."""
    view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    view.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    surface_format = view.format()
    surface_format.setAlphaBufferSize(8)
    view.setFormat(surface_format)
    view.setClearColor(Qt.GlobalColor.transparent)
    if properties:
        view.setInitialProperties(properties)
    component = Path(__file__).resolve().with_name("qml") / filename
    view.setSource(QUrl.fromLocalFile(str(component)))
    if view.status() != QQuickWidget.Status.Ready:
        details = "\n".join(error.toString() for error in view.errors())
        raise RuntimeError(f"Cannot load Qt Quick surface {filename}: {details}")
