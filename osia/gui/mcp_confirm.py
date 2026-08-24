#!/usr/bin/env python3
"""WOR-44 Phase 5 (5-conf-b) — DESKTOP confirmation provider for write MCP tools.

The headless gate (``osia.mcp.registry``, 5-conf-a) calls a registered provider
``provider(server_id, tool_name, summary) -> bool`` at the top of every
write/destructive MCP tool. This module supplies the desktop (PyQt) provider so
the user can approve or decline on screen.

Threading model (the crux)
---------------------------
The agent loop — and therefore the gate and this provider — runs on a daemon
WORKER thread (the tool executor), while every QWidget must be created/shown on
the Qt MAIN thread. ``MCPConfirmController.confirm`` is the provider; it is
CALLED ON THE WORKER THREAD and must block it until the user answers WITHOUT
freezing the UI. It does so with the established cross-thread bridge:

  * a ``threading.Event`` + a result holder are created on the worker thread,
  * a QUEUED ``pyqtSignal`` carries the request to the main thread,
  * an exactly correlated active Chat turn receives a compact inline prompt;
    legacy/background flows retain the established modal fallback,
  * the worker blocks on ``event.wait(timeout=...)``,
  * the main-thread slot shows the modal (``exec`` — a nested event loop, so the
    rest of the UI stays responsive), and on accept/reject sets the holder +
    ``event.set()``,
  * a GUI-thread auto-decline ``QTimer`` rejects the dialog at the timeout, so a
    walked-away user safely DENIES instead of hanging the worker.

Safety contract: ``confirm`` returns ``True`` ONLY on an explicit approve.
Decline, Esc, timeout, a missing GUI, or ANY error all return ``False`` — the
gate then blocks the write (the safe default). Confirmations are serialized
(one dialog at a time) by a lock, so two worker threads can never stack dialogs.
"""

from __future__ import annotations

import secrets
import threading

from PyQt6.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from osia import i18n


# Confirmation wait budget. Kept comfortably UNDER the per-call MCP tool budget
# granted in 5-conf-a (osia.agent._MCP_CONFIRM_TOOL_TIMEOUT == 150 s) so the
# dialog's own auto-decline fires BEFORE the executor would kill the tool call.
_DEFAULT_CONFIRM_TIMEOUT = 120.0
# Worker-side backstop: a touch beyond the dialog timeout so the GUI auto-decline
# normally resolves first; this only fires if the GUI thread is wedged -> deny.
_WORKER_WAIT_GRACE = 15.0
# Bound the args summary shown in the dialog (it carries arg NAMES only).
_SUMMARY_MAX = 300


class _PendingConfirm:
    """Carries one confirmation request from the worker thread to the GUI slot,
    plus the synchronization primitives the worker blocks on."""

    __slots__ = (
        "server", "tool", "summary", "event", "holder",
        "permission_request_id", "binding", "attempt_id", "attempt_store",
    )

    def __init__(
        self,
        server,
        tool,
        summary,
        event,
        holder,
        *,
        permission_request_id,
        binding,
        attempt_id,
        attempt_store,
    ):
        self.server = server
        self.tool = tool
        self.summary = summary
        self.event = event
        self.holder = holder
        self.permission_request_id = permission_request_id
        self.binding = binding
        self.attempt_id = attempt_id
        self.attempt_store = attempt_store


class _MCPConfirmDialog(QDialog):
    """Frameless, dark, always-on-top modal asking the user to approve/decline a
    write MCP tool. Mirrors the ``_TokenDialog`` / ``_PopoverDialog`` idiom so it
    inherits the popup's ``dialogCard`` stylesheet. Decline is the SAFE default:
    Esc rejects (QDialog default) and focus starts on Decline, so an accidental
    Enter never approves."""

    def __init__(self, parent, server_id, tool_name, summary):
        super().__init__(parent)
        self.setObjectName("mcpConfirmDialog")
        # The parent popup is FramelessWindowHint|Tool|WindowStaysOnTopHint — a
        # modal child that is not itself always-on-top can paint BEHIND it on
        # some X11/Wayland WMs (reads as a frozen UI). Mirror the existing
        # dialogs' flags so the modal always surfaces above the parent.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("dialogCard")
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        title = QLabel(i18n.t("mcp.confirm.title"))
        title.setObjectName("dialogTitle")
        title.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(title)

        body = QLabel(
            i18n.t("mcp.confirm.body", server=str(server_id), tool=str(tool_name))
        )
        body.setObjectName("sectionSub")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setMinimumWidth(320)
        lay.addWidget(body)

        detail = (str(summary) if summary else "").strip()
        if len(detail) > _SUMMARY_MAX:
            detail = detail[: _SUMMARY_MAX - 1] + "…"
        if detail:
            dl = QLabel(detail)
            dl.setObjectName("sectionSub")
            dl.setTextFormat(Qt.TextFormat.PlainText)
            dl.setWordWrap(True)
            lay.addWidget(dl)

        warn = QLabel(i18n.t("mcp.confirm.warn"))
        warn.setObjectName("sectionSub")
        warn.setTextFormat(Qt.TextFormat.PlainText)
        warn.setWordWrap(True)
        lay.addWidget(warn)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        decline = QPushButton(i18n.t("mcp.confirm.decline"))
        decline.setObjectName("ghostBtn")
        decline.setCursor(Qt.CursorShape.PointingHandCursor)
        decline.clicked.connect(self.reject)
        approve = QPushButton(i18n.t("mcp.confirm.approve"))
        approve.setObjectName("primaryBtn")
        approve.setCursor(Qt.CursorShape.PointingHandCursor)
        approve.clicked.connect(self.accept)
        btn_row.addWidget(decline)
        btn_row.addWidget(approve)
        lay.addLayout(btn_row)

        # Decline is the default action: Enter triggers it, and it holds initial
        # focus — only a deliberate click on Approve (or its activation) approves.
        decline.setDefault(True)
        approve.setAutoDefault(False)
        self._decline_btn = decline

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        if parent is not None:
            try:
                geo = parent.window().frameGeometry()
                self.move(geo.center() - self.rect().center())
            except Exception:
                pass
        # Guarantee the modal surfaces above the always-on-top parent.
        self.raise_()
        self.activateWindow()
        try:
            self._decline_btn.setFocus()
        except Exception:
            pass


class MCPConfirmController(QObject):
    """GUI-side controller whose ``confirm`` is registered as the MCP
    confirmation provider (``registry.set_confirmation_provider``).

    Created on the GUI thread (so the queued signal's slot runs there). Keep a
    reference alive for the lifetime of the app — dropping it disconnects the
    signal and silently reverts the gate to hard-deny.
    """

    # Carries a _PendingConfirm to the GUI thread. A cross-thread emit uses a
    # QUEUED connection by default, so _show_request ALWAYS runs on this object's
    # (GUI) thread — never the worker.
    _request = pyqtSignal(object)

    def __init__(self, parent_window, timeout: float = _DEFAULT_CONFIRM_TIMEOUT):
        super().__init__()
        self._parent_window = parent_window
        self._timeout = float(timeout)
        # Serialize confirmations: at most one dialog at a time. Tool calls are
        # sequential per turn, but the executor has multiple workers, so the lock
        # makes concurrent providers safe (the 2nd waits for the 1st to resolve).
        self._gate_lock = threading.Lock()
        self._request.connect(self._show_request)
        # The stronger deletion owner shares the same GUI-thread lifetime but
        # keeps its private plan and non-truncating view in a dedicated module.
        self._deletion_controller = None
        try:
            from osia.action_policy import (
                set_reinforced_deletion_confirmation_provider,
            )
            from osia.gui.deletion_confirm import DeletionConfirmController

            self._deletion_controller = DeletionConfirmController(parent_window)
            set_reinforced_deletion_confirmation_provider(
                self._deletion_controller.confirm
            )
        except Exception:
            self._deletion_controller = None

    # ── Provider entry point (CALLED ON THE WORKER THREAD) ───────────────────
    def confirm(self, server_id: str, tool_name: str, summary: str) -> bool:
        """Return ``True`` only on an explicit approve; ``False`` on decline /
        timeout / no-GUI / any error (the gate then blocks the write). Never
        raises (the provider contract)."""
        try:
            app = QApplication.instance()
            if app is None:
                return False  # no GUI running -> deny (safe)
            with self._gate_lock:  # one dialog at a time
                binding, attempt_id, attempt_store = self._trusted_chat_attempt()
                # An empty mapping is the fail-closed marker for a Chat
                # ToolAttempt whose exact durable binding could not be proven.
                # It must never be downgraded to the legacy modal path.
                if binding == {}:
                    return False
                # Defensive: if we are somehow ON the GUI thread, the worker-wait
                # bridge would deadlock (the slot can't run while we block the
                # loop). Exact Chat permissions are inline-only, so deny them;
                # the modal remains available solely to genuine non-Chat calls.
                if QThread.currentThread() == app.thread():
                    if binding is not None:
                        return False
                    approved = self._run_modal(
                        self._parent_window, server_id, tool_name, summary
                    )
                    return bool(
                        approved
                        and (
                            binding is None
                            or self._attempt_binding_is_current(
                                attempt_store, attempt_id, binding,
                            )
                        )
                    )
                event = threading.Event()
                holder = {"approved": False, "closed": False}
                pending = _PendingConfirm(
                    server_id,
                    tool_name,
                    summary,
                    event,
                    holder,
                    permission_request_id=(
                        "pc_permission_" + secrets.token_urlsafe(24)
                    ),
                    binding=binding,
                    attempt_id=attempt_id,
                    attempt_store=attempt_store,
                )
                self._request.emit(pending)
                # Backstop just beyond the dialog's own auto-decline so the GUI
                # timer resolves first; this only fires if the GUI is wedged.
                finished = event.wait(timeout=self._timeout + _WORKER_WAIT_GRACE)
                if not finished:
                    holder["closed"] = True
                    return False
                if not holder.get("approved"):
                    return False
                return bool(
                    binding is None
                    or self._attempt_binding_is_current(
                        attempt_store, attempt_id, binding,
                    )
                )
        except Exception:
            return False  # any failure -> deny (safe)

    # ── GUI-thread slot ──────────────────────────────────────────────────────
    def _show_request(self, pending: "_PendingConfirm") -> None:
        """Project an exact Chat prompt, otherwise use the modal fallback."""
        if pending.holder.get("closed"):
            return
        if pending.binding is not None:
            presenter = getattr(
                self._parent_window, "present_inline_mcp_confirmation", None,
            )
            if callable(presenter):
                payload = {
                    **pending.binding,
                    "permission_request_id": pending.permission_request_id,
                    "server_id": str(pending.server or ""),
                    "tool_name": str(pending.tool or ""),
                    "summary": str(pending.summary or ""),
                }

                def _resolve_inline(approved: bool) -> None:
                    self._settle_pending(pending, approved)

                try:
                    if presenter(
                        payload,
                        _resolve_inline,
                        timeout_ms=max(1, int(self._timeout * 1000)),
                    ):
                        return
                except Exception:
                    pass
            # A correlated Chat permission is valid only inside its exact,
            # visible Thinking block. Hidden/background/stale UI therefore
            # declines instead of opening an unbound modal.
            self._settle_pending(pending, False)
            return
        approved = False
        try:
            approved = self._run_modal(
                self._parent_window, pending.server, pending.tool, pending.summary
            )
        except Exception:
            approved = False
        self._settle_pending(pending, approved)

    @staticmethod
    def _trusted_chat_attempt():
        """Capture a trusted active Chat ToolAttempt or request modal fallback."""
        attempt_id = ""
        store = None
        try:
            from osia.tool_attempts import (
                TERMINAL_STATUSES,
                ToolAttemptStatus,
                current_tool_attempt_id,
                current_tool_attempt_store,
            )

            attempt_id = str(current_tool_attempt_id() or "")
            store = current_tool_attempt_store()
            if not attempt_id or store is None:
                return None, "", None
            record = store.get_attempt(attempt_id) if store and attempt_id else None
            if not isinstance(record, dict):
                return {}, attempt_id, store
            if (
                str(record.get("tool_attempt_id") or "") != attempt_id
                or ToolAttemptStatus(record.get("status")) in (
                    TERMINAL_STATUSES
                    | {ToolAttemptStatus.CANCELLATION_REQUESTED}
                )
            ):
                return {}, attempt_id, store
            if record.get("owner_scope") != "chat":
                return None, "", None
            from osia.turn_terminality import load_persisted_execution_binding

            binding = load_persisted_execution_binding(
                str(record.get("turn_id") or "")
            )
            if (
                not isinstance(binding, dict)
                or any(
                    str(record.get(name) or "") != str(binding.get(name) or "")
                    for name in (
                        "conversation_id", "request_id", "turn_id",
                        "provider_id", "model_id",
                    )
                )
                or any(not str(binding.get(name) or "") for name in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                ))
                or not str(record.get("final_response_message_id") or "")
            ):
                return {}, attempt_id, store
            return binding, attempt_id, store
        except Exception:
            return (
                ({}, attempt_id, store)
                if attempt_id else
                (None, "", None)
            )

    @staticmethod
    def _attempt_binding_is_current(store, attempt_id: str, binding: dict) -> bool:
        try:
            from osia.tool_attempts import TERMINAL_STATUSES, ToolAttemptStatus
            from osia.turn_terminality import load_persisted_execution_binding

            record = store.get_attempt(attempt_id) if store and attempt_id else None
            persisted_binding = load_persisted_execution_binding(
                str(binding.get("turn_id") or "")
            )
            return bool(
                isinstance(record, dict)
                and isinstance(persisted_binding, dict)
                and persisted_binding == binding
                and record.get("owner_scope") == "chat"
                and str(record.get("tool_attempt_id") or "") == attempt_id
                and ToolAttemptStatus(record.get("status")) not in TERMINAL_STATUSES
                and ToolAttemptStatus(record.get("status"))
                is not ToolAttemptStatus.CANCELLATION_REQUESTED
                and all(
                    str(record.get(name) or "") == expected
                    for name, expected in (
                        ("conversation_id", binding["conversation_id"]),
                        ("request_id", binding["request_id"]),
                        ("turn_id", binding["turn_id"]),
                        ("provider_id", binding["provider_id"]),
                        ("model_id", binding["model_id"]),
                    )
                )
                and bool(str(record.get("final_response_message_id") or ""))
            )
        except Exception:
            return False

    def _settle_pending(self, pending: "_PendingConfirm", approved: bool) -> None:
        if pending.holder.get("closed"):
            return
        accepted = bool(
            approved
            and (
                pending.binding is None
                or self._attempt_binding_is_current(
                    pending.attempt_store,
                    pending.attempt_id,
                    pending.binding,
                )
            )
        )
        pending.holder["approved"] = accepted
        pending.holder["closed"] = True
        pending.event.set()

    def _run_modal(self, parent, server_id, tool_name, summary) -> bool:
        """Build + exec the modal on the CURRENT (GUI) thread. An auto-decline
        timer rejects it at the timeout so a walked-away user safely denies."""
        dlg = _MCPConfirmDialog(parent, server_id, tool_name, summary)
        timer = QTimer(dlg)
        timer.setSingleShot(True)
        timer.timeout.connect(dlg.reject)
        timer.start(int(self._timeout * 1000))
        try:
            result = dlg.exec()
        finally:
            timer.stop()
        return result == QDialog.DialogCode.Accepted
