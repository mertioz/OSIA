"""Local pointer cursors for OSIA controls, including dynamically rebuilt pages."""

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractButton, QApplication, QComboBox


class ButtonCursorPolicy(QObject):
    """Keep controls consistent without overriding the system-wide cursor."""

    def __init__(self, root):
        super().__init__(root)
        self._root = root
        self._root_alive = True
        root.destroyed.connect(self._root_destroyed)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        for control in root.findChildren(QAbstractButton) + root.findChildren(QComboBox):
            self._apply(control)

    @staticmethod
    def _apply(control) -> None:
        control.setCursor(
            Qt.CursorShape.PointingHandCursor if control.isEnabled()
            else Qt.CursorShape.ArrowCursor
        )

    def _root_destroyed(self, _object=None) -> None:
        self._root_alive = False

    def eventFilter(self, watched, event):  # noqa: N802 (Qt method name)
        if (
            event.type() in (QEvent.Type.Polish, QEvent.Type.Show, QEvent.Type.EnabledChange)
            and isinstance(watched, (QAbstractButton, QComboBox))
            and self._root_alive
            and self._root.isAncestorOf(watched)
        ):
            self._apply(watched)
        return False
