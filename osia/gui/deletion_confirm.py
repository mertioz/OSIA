"""Local-only reinforced confirmation for a sealed colossal DeletionPlan."""

from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeView,
    QVBoxLayout,
)

from osia import i18n
from osia.deletion_plan import DeletionInventoryEntry, DeletionPlan


_DEFAULT_TIMEOUT_SECONDS = 25.0
_WORKER_WAIT_GRACE_SECONDS = 5.0


def _human_size(value: int) -> str:
    amount = float(max(0, int(value)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return "0 B"


class _TreeNode:
    __slots__ = ("label", "kind", "size", "parent", "children")

    def __init__(self, label: str, kind: str = "", size: int = 0, parent=None):
        self.label = label
        self.kind = kind
        self.size = int(size)
        self.parent = parent
        self.children: list[_TreeNode] = []

    def child_named(self, name: str):
        return next((child for child in self.children if child.label == name), None)


class DeletionInventoryModel(QAbstractItemModel):
    """Hierarchical, non-truncating model; QTreeView virtualizes visible rows."""

    def __init__(self, plan: DeletionPlan, parent=None):
        if not isinstance(plan, DeletionPlan):
            raise ValueError("untrusted_deletion_plan")
        super().__init__(parent)
        self._root = _TreeNode("")
        self.total_entries = len(plan._local_entries())
        roots: list[_TreeNode] = []
        for private_root in plan._local_roots():
            node = _TreeNode(private_root, "root", parent=self._root)
            self._root.children.append(node)
            roots.append(node)
        for entry in plan._local_entries():
            self._insert_entry(roots[entry.root_index], entry)

    @staticmethod
    def _parts(entry: DeletionInventoryEntry) -> list[str]:
        if entry.relative_path in {"", "."}:
            return []
        return [part for part in entry.relative_path.replace("\\", "/").split("/") if part]

    def _insert_entry(self, root: _TreeNode, entry: DeletionInventoryEntry) -> None:
        parts = self._parts(entry)
        if not parts:
            root.kind = entry.entry_type.value
            root.size = entry.size
            return
        node = root
        for index, part in enumerate(parts):
            child = node.child_named(part)
            if child is None:
                child = _TreeNode(part, parent=node)
                node.children.append(child)
                node.children.sort(key=lambda item: item.label.casefold())
            node = child
            if index == len(parts) - 1:
                node.kind = entry.entry_type.value
                node.size = entry.size

    def columnCount(self, parent=QModelIndex()):
        del parent
        return 3

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid() and parent.column() != 0:
            return 0
        node = parent.internalPointer() if parent.isValid() else self._root
        return len(node.children)

    def index(self, row, column, parent=QModelIndex()):
        if row < 0 or column < 0 or column >= 3:
            return QModelIndex()
        node = parent.internalPointer() if parent.isValid() else self._root
        if row >= len(node.children):
            return QModelIndex()
        return self.createIndex(row, column, node.children[row])

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        parent = node.parent
        if parent is None or parent is self._root:
            return QModelIndex()
        grandparent = parent.parent or self._root
        return self.createIndex(grandparent.children.index(parent), 0, parent)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        node = index.internalPointer()
        if index.column() == 0:
            return node.label
        if index.column() == 1:
            return node.kind
        if index.column() == 2 and node.size:
            return _human_size(node.size)
        return ""

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return (i18n.t("deletion.confirm.name"), i18n.t("deletion.confirm.type"), i18n.t("deletion.confirm.size"))[section]
        return None


class _ClickOnlyButton(QPushButton):
    """Button that ignores keyboard activation for the irreversible action."""

    def keyPressEvent(self, event) -> None:
        event.ignore()


class ReinforcedDeletionDialog(QDialog):
    """Danger modal whose Allow path can only be reached by button activation."""

    def __init__(self, parent, plan: DeletionPlan):
        if not isinstance(plan, DeletionPlan) or not plan.colossal_deletion:
            raise ValueError("reinforced_dialog_requires_colossal_plan")
        super().__init__(parent)
        self._allow_clicked = False
        self.setObjectName("reinforcedDeletionDialog")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.resize(780, 620)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("dialogCard")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(10)

        title = QLabel(i18n.t("deletion.confirm.title"))
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        warning = QLabel(i18n.t("deletion.confirm.warning"))
        warning.setObjectName("sectionSub")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        summary = QLabel(i18n.t(
            "deletion.confirm.summary",
            files=plan.files_count,
            folders=plan.folders_count,
            size=_human_size(plan.total_bytes),
            roots=plan.root_count,
        ))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if plan.recursive:
            recursive = QLabel(i18n.t("deletion.confirm.recursive"))
            recursive.setObjectName("sectionSub")
            recursive.setWordWrap(True)
            layout.addWidget(recursive)
        expiry = QLabel(i18n.t("deletion.confirm.expires"))
        expiry.setObjectName("sectionSub")
        expiry.setWordWrap(True)
        layout.addWidget(expiry)

        self.inventory_model = DeletionInventoryModel(plan, self)
        tree = QTreeView()
        tree.setObjectName("deletionInventoryTree")
        tree.setModel(self.inventory_model)
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        tree.setSortingEnabled(False)
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(tree, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton(i18n.t("deletion.confirm.cancel"))
        cancel.setObjectName("ghostBtn")
        cancel.clicked.connect(self.reject)
        allow = _ClickOnlyButton(i18n.t("deletion.confirm.allow"))
        allow.setObjectName("dangerBtn")
        allow.setAutoDefault(False)
        allow.setDefault(False)
        allow.clicked.connect(self._allow)
        buttons.addWidget(cancel)
        buttons.addWidget(allow)
        layout.addLayout(buttons)
        self._cancel_button = cancel
        self._allow_button = allow

    def _allow(self) -> None:
        self._allow_clicked = True
        super().accept()

    def accept(self) -> None:
        if self._allow_clicked:
            super().accept()

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            event.ignore()
            return
        super().keyPressEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._cancel_button.setFocus()
        self.raise_()
        self.activateWindow()


class _PendingReview:
    __slots__ = ("plan", "event", "holder")

    def __init__(self, plan, event, holder):
        self.plan = plan
        self.event = event
        self.holder = holder


class DeletionConfirmController(QObject):
    _request = pyqtSignal(object)

    def __init__(self, parent_window, timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        super().__init__()
        self._parent_window = parent_window
        self._timeout = float(timeout)
        self._gate_lock = threading.Lock()
        self._request.connect(self._show_dialog)

    def confirm(self, plan: DeletionPlan) -> bool:
        try:
            if not isinstance(plan, DeletionPlan) or not plan.colossal_deletion:
                return False
            if plan.expires_at <= time.time():
                return False
            app = QApplication.instance()
            if app is None:
                return False
            with self._gate_lock:
                if QThread.currentThread() == app.thread():
                    return self._run_modal(plan)
                event = threading.Event()
                holder = {"approved": False}
                self._request.emit(_PendingReview(plan, event, holder))
                finished = event.wait(self._timeout + _WORKER_WAIT_GRACE_SECONDS)
                return bool(holder.get("approved")) if finished else False
        except Exception:
            return False

    def _show_dialog(self, pending: _PendingReview) -> None:
        approved = False
        try:
            if pending.plan.expires_at > time.time():
                approved = self._run_modal(pending.plan)
        finally:
            pending.holder["approved"] = bool(approved)
            pending.event.set()

    def _run_modal(self, plan: DeletionPlan) -> bool:
        if plan.expires_at <= time.time():
            return False
        dialog = ReinforcedDeletionDialog(self._parent_window, plan)
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(dialog.reject)
        timer.start(int(self._timeout * 1000))
        try:
            result = dialog.exec()
        finally:
            timer.stop()
        return bool(
            result == QDialog.DialogCode.Accepted
            and dialog._allow_clicked
        )


__all__ = [
    "DeletionConfirmController",
    "DeletionInventoryModel",
    "ReinforcedDeletionDialog",
]
