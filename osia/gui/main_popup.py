#!/usr/bin/env python3
"""
OSIA Main Popup — floating panel that opens below the Dynamic Island.

The popup is a frameless, always-on-top dark-glassmorphism panel
containing:

* a chat history view (the default page),
* a bottom text input row with file/send buttons,
* a 3-icon bottom nav (Conversations / Model / Settings).

Each nav icon swaps the central content with a dedicated panel
implemented in :mod:`osia.gui.panels`.

The popup itself is thin glue — message rendering, panel state, and the
agent live in the host application (``osia/main.py``). The popup exposes
signals/slots so the host can wire up the actual behaviour without the
GUI knowing about threads or persistence.
"""

import math
import re
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QEvent, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QPoint,
    QRectF, QPointF,
)
from PyQt6.QtGui import (
    QColor, QPainter, QFont, QPainterPath, QGuiApplication, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QStackedWidget, QFrame, QGraphicsDropShadowEffect,
    QSizePolicy, QFileDialog, QCheckBox, QComboBox,
    QButtonGroup, QGridLayout, QDialog, QTextEdit, QInputDialog,
)

from osia import conversations
from osia import personality
from osia.agent import strip_tool_artifacts
from osia.models import (
    PROVIDERS, get_all_models,
)
from osia.config import (
    load_api_keys, save_api_keys, load_settings, set_setting, set_api_key,
    get_api_key, load_mobile_token, regenerate_mobile_token,
)
from osia.utils.systemd_manager import (
    is_service_enabled, enable_service, disable_service, get_service_status,
    restart_service,
)
from osia.utils.assets import LOGO_PATH, autostart_enabled, set_autostart
from osia import i18n
from osia import timefmt

# sip lets us detect widgets whose C++ side has already been destroyed
# (accessing them raises RuntimeError otherwise). It ships with PyQt6 but
# guard the import so a missing module never breaks the GUI.
try:
    from PyQt6 import sip
except ImportError:
    sip = None


# ── Visual constants ────────────────────────────────────────────────────────

POPUP_W = 560
POPUP_H = 680
RADIUS = 20
# Dark, semi-transparent glassmorphism fill so the popup reads as a floating
# glass panel rather than an opaque window. Lower alpha → more transparent;
# a second, slightly lighter layer painted on top fakes a soft blur/frost.
BG_COLOR = QColor(13, 11, 18, 220)
BG_BLUR_LAYER = QColor(26, 22, 37, 70)      # #1a1625 wash for a frosted feel
BORDER_COLOR = QColor(196, 181, 245, 70)    # subtle violet edge
PANEL_BG = QColor(255, 255, 255, 10)
ACCENT = QColor(196, 181, 245)      # #c4b5f5
ACCENT_DARK = QColor(140, 110, 220)
SUCCESS = QColor(105, 200, 140)
TEXT = QColor(245, 245, 250)
TEXT_DIM = QColor(245, 245, 250, 150)
DIVIDER = QColor(255, 255, 255, 18)

STARTUP_SERVICES = ("osia-server.service", "osia-tunnel.service")

# Official API-key pages opened by the "Get API key" → Browse button on each
# provider's configuration card. Ollama is local and has no key page.
PROVIDER_KEY_URLS = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai":    "https://platform.openai.com/api-keys",
    "google":    "https://aistudio.google.com/apikey",
    "deepseek":  "https://platform.deepseek.com/api_keys",
    "xai":       "https://console.x.ai",
    "groq":      "https://console.groq.com/keys",
}

# Per-provider accent colors used for the dot indicators on the model panel
# provider grid and the settings provider rows.
PROVIDER_COLORS = {
    "anthropic": "#d97757",   # Anthropic clay
    "openai":    "#10a37f",   # OpenAI teal
    "deepseek":  "#4d6bfe",   # DeepSeek blue
    "google":    "#4285f4",   # Google blue
    "xai":       "#cfcfcf",   # xAI light
    "groq":      "#f55036",   # Groq orange
    "ollama":    "#69c88c",   # Local green
}

# Providers with a free tier worth calling out — provider key → i18n key of
# the tooltip shown by the "!" info badge next to the provider's name in the
# model panel grid and the settings provider card.
FREETIER_INFO_KEYS = {
    "google": "google_freetier_info",
    "groq":   "groq_freetier_info",
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _format_time(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d %b ") + timefmt.format_time(dt)
    except (ValueError, TypeError):
        return ts


def _is_deleted(widget) -> bool:
    """True when *widget*'s underlying C++ object has been destroyed.

    Qt can delete child widgets from the C++ side (e.g. while a layout is
    cleared); the Python wrapper then raises RuntimeError on any access.
    """
    if widget is None:
        return True
    if sip is not None:
        try:
            return sip.isdeleted(widget)
        except TypeError:
            pass
    return False


def _display_text(content) -> str:
    """Reduce a stored message content to its displayable text.

    Returns "" when the message must not be rendered at all:
    - internal tool-result feedback (a string starting with
      ``<tool_result>``, or a vision content-parts list),
    - content that is empty once every tool artifact (``<tool_call>``
      blocks, bare ``{"tool": ...}`` JSON, reasoning blocks, Llama special
      tokens) is stripped.

    An empty message rendered as a bubble shows up as a phantom thin
    violet line in the chat — callers MUST skip messages that come back
    empty here.

    Guard against over-stripping: ``strip_tool_artifacts`` returns the raw
    text (and logs a warning) when stripping would empty a message that
    doesn't actually look like tool/reasoning markup — a normal response
    must never silently vanish from the chat.
    """
    if content is None:
        return ""
    # Vision tool feedback stores content as a list of parts — internal.
    if not isinstance(content, str):
        return ""
    if content.lstrip().startswith("<tool_result>"):
        return ""
    return strip_tool_artifacts(content)


def _mask_token(token: str) -> str:
    """Mask a pairing token for display, keeping only the first/last 2 chars
    (e.g. ``02****************dk``). Short or empty tokens are masked safely.
    """
    if not token:
        return "—"
    if len(token) <= 6:
        return token[:1] + "*" * max(1, len(token) - 1)
    return token[:2] + "*" * (len(token) - 4) + token[-2:]


# faster-whisper caches each model in the Hugging Face hub cache under a
# directory named ``models--Systran--faster-whisper-<size>``. We probe it so the
# local Whisper model selector only offers (or only enables) the sizes that are
# actually downloaded on this machine.
_FASTER_WHISPER_REPO_PREFIX = "models--Systran--faster-whisper-"


def _downloaded_whisper_models() -> set[str]:
    """Return the faster-whisper model sizes present in the local HF hub cache.

    Looks for ``~/.cache/huggingface/hub/models--Systran--faster-whisper-<size>``
    directories. Never raises — an absent or unreadable cache yields an empty
    set, so the selector simply offers nothing as downloaded.
    """
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    found: set[str] = set()
    try:
        for entry in hub.iterdir():
            if entry.is_dir() and entry.name.startswith(_FASTER_WHISPER_REPO_PREFIX):
                found.add(entry.name[len(_FASTER_WHISPER_REPO_PREFIX):])
    except OSError:
        pass
    return found


# ── Message bubble ──────────────────────────────────────────────────────────

class _Bubble(QFrame):
    """One chat message bubble — user (right, violet) or assistant (left)."""

    def __init__(self, role: str, text: str):
        super().__init__()
        self.setObjectName("msgBubble")
        self.setProperty("role", role)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        label = QLabel(text or "")
        label.setObjectName("msgText")
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(label)

        self._label = label
        self.setMaximumWidth(360)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)

    def append_text(self, more: str) -> None:
        self._label.setText(self._label.text() + more)


# ── Chat page ───────────────────────────────────────────────────────────────

class _ChatPage(QWidget):
    """Scrollable message list + text input row at the bottom."""

    send_text = pyqtSignal(str)
    pick_file = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scrollable history
        self._scroll = QScrollArea()
        self._scroll.setObjectName("chatScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._messages_host = QWidget()
        self._messages_host.setObjectName("chatHost")
        self._messages_layout = QVBoxLayout(self._messages_host)
        self._messages_layout.setContentsMargins(14, 14, 14, 14)
        self._messages_layout.setSpacing(8)
        self._messages_layout.addStretch(1)
        self._scroll.setWidget(self._messages_host)
        outer.addWidget(self._scroll, 1)

        # Empty-state placeholder, swapped out as soon as a message arrives.
        self._empty = self._build_empty_label()
        self._empty_idx = self._messages_layout.count() - 1
        self._messages_layout.insertWidget(self._empty_idx, self._empty)

        # Input row
        input_wrap = QFrame()
        input_wrap.setObjectName("inputWrap")
        row = QHBoxLayout(input_wrap)
        row.setContentsMargins(10, 8, 10, 10)
        row.setSpacing(8)

        self._file_btn = QPushButton("+")
        self._file_btn.setObjectName("iconBtn")
        self._file_btn.setFixedSize(34, 34)
        self._file_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._file_btn.setToolTip(i18n.t("chat.attach_file"))
        self._file_btn.clicked.connect(self._on_pick_file)

        self._input = QLineEdit()
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText(i18n.t("chat.input_placeholder"))
        self._input.returnPressed.connect(self._on_send)

        self._send_btn = QPushButton("➤")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setFixedSize(34, 34)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setToolTip(i18n.t("chat.send"))
        self._send_btn.clicked.connect(self._on_send)

        row.addWidget(self._file_btn)
        row.addWidget(self._input, 1)
        row.addWidget(self._send_btn)
        outer.addWidget(input_wrap)

        # Re-translate the input chrome + empty-state label on language change.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

    def _retranslate(self) -> None:
        self._file_btn.setToolTip(i18n.t("chat.attach_file"))
        self._input.setPlaceholderText(i18n.t("chat.input_placeholder"))
        self._send_btn.setToolTip(i18n.t("chat.send"))
        if not _is_deleted(self._empty):
            self._empty.setText(i18n.t("chat.empty"))

    # ── Public API ────────────────────────────────────────────────────────

    def clear_messages(self) -> None:
        # Remove every widget except the trailing stretch + empty label.
        # Every Qt access is guarded: widgets can already be deleted on the
        # C++ side (e.g. when the last conversation is removed), and touching
        # them raises RuntimeError ("wrapped C/C++ object ... deleted").
        try:
            while self._messages_layout.count() > 1:
                item = self._messages_layout.takeAt(0)
                w = item.widget()
                # Never delete the placeholder itself — deleting it would
                # leave self._empty wrapping a dead C++ object.
                if w is not None and w is not self._empty:
                    w.deleteLater()
        except RuntimeError:
            pass
        if _is_deleted(self._empty):
            # The placeholder was destroyed earlier — build a fresh one.
            self._empty = self._build_empty_label()
        try:
            self._empty.show()
            self._messages_layout.insertWidget(0, self._empty)
        except RuntimeError:
            pass

    def set_messages(self, messages: list[dict]) -> None:
        self.clear_messages()
        # Keep only real user/assistant turns with visible text once tool
        # artifacts (tool_call blocks, bare {"tool": ...} JSON, reasoning
        # blocks…) are stripped. Messages that come back empty are dropped
        # entirely — an empty bubble renders as a phantom thin violet line.
        renderable = []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                continue
            text = _display_text(m.get("content"))
            if text:
                renderable.append((m["role"], text))
        if not renderable:
            return
        try:
            if not _is_deleted(self._empty):
                self._empty.hide()
            for role, text in renderable:
                self._append_bubble(role, text)
            QTimer.singleShot(0, self._scroll_to_bottom)
        except RuntimeError:
            pass

    def append_message(self, role: str, text: str) -> None:
        # Strip tool artifacts before display; skip the message entirely if
        # nothing visible remains (empty bubble = phantom violet line).
        text = _display_text(text)
        if not text:
            return
        self._empty.hide()
        self._append_bubble(role, text)
        QTimer.singleShot(0, self._scroll_to_bottom)

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_empty_label() -> QLabel:
        label = QLabel(i18n.t("chat.empty"))
        label.setObjectName("chatEmpty")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _append_bubble(self, role: str, text: str) -> None:
        # Defense in depth: never create a bubble (or its wrapper row
        # widget) for empty text — a blank QFrame collapses into a thin
        # violet line in the chat.
        if not text or not str(text).strip():
            return
        bubble = _Bubble(role, text)
        align = (Qt.AlignmentFlag.AlignRight if role == "user"
                 else Qt.AlignmentFlag.AlignLeft)
        # Insert just before the trailing stretch so the chat grows down.
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        # Insert before the trailing stretch.
        self._messages_layout.insertWidget(
            self._messages_layout.count() - 1, wrap
        )

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.send_text.emit(text)

    def _on_pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, i18n.t("chat.attach_file"))
        if path:
            self.pick_file.emit(path)


# ── Conversations panel ────────────────────────────────────────────────────

class _ConvRow(QFrame):
    """One row in the conversations list."""

    open_clicked = pyqtSignal(str)
    delete_clicked = pyqtSignal(str)

    def __init__(self, conv: dict, is_active: bool = False):
        super().__init__()
        self.setObjectName("convRow")
        self.setProperty("active", is_active)
        self._conv_id = conv.get("id", "")

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 10, 10)
        row.setSpacing(8)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)
        self._full_title = conv.get("title") or i18n.t("conversations.new_conversation")
        title = QLabel(self._full_title)
        title.setObjectName("convTitle")
        title.setWordWrap(False)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # Width is ignored so a long title never widens the row; it is elided
        # with an ellipsis (…) in resizeEvent instead of overflowing.
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._title_label = title
        sub = QLabel(
            i18n.t("conversations.msg_count", count=conv.get("message_count", 0))
            + " · "
            + _format_time(conv.get("updated_at"))
        )
        sub.setObjectName("convSub")
        title_col.addWidget(title)
        title_col.addWidget(sub)

        open_btn = QPushButton(i18n.t("conversations.open"))
        open_btn.setObjectName("convOpen")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(lambda: self.open_clicked.emit(self._conv_id))

        del_btn = QPushButton("✕")
        del_btn.setObjectName("convDel")
        del_btn.setFixedSize(26, 26)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(lambda: self.delete_clicked.emit(self._conv_id))

        row.addLayout(title_col, 1)
        row.addWidget(open_btn)
        row.addWidget(del_btn)

    def resizeEvent(self, event):
        fm = self._title_label.fontMetrics()
        self._title_label.setText(
            fm.elidedText(
                self._full_title, Qt.TextElideMode.ElideRight,
                max(0, self._title_label.width()),
            )
        )
        super().resizeEvent(event)


class _ConversationsPanel(QWidget):
    """List of past conversations + 'new chat' action."""

    new_conversation = pyqtSignal()
    open_conversation = pyqtSignal(str)
    delete_conversation = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._active_id: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        self._title = QLabel(i18n.t("conversations.title"))
        self._title.setObjectName("panelTitle")
        self._new_btn = QPushButton("＋ " + i18n.t("conversations.new_chat"))
        self._new_btn.setObjectName("primaryBtn")
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self.new_conversation.emit)
        header.addWidget(self._title)
        header.addStretch()
        header.addWidget(self._new_btn)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._host = QWidget()
        # QScrollArea.setWidget() forces autoFillBackground(True) on the host,
        # which paints the default grey palette. Name it so the stylesheet can
        # force it back to transparent and keep the app's dark background.
        self._host.setObjectName("panelHost")
        self._host_layout = QVBoxLayout(self._host)
        self._host_layout.setContentsMargins(0, 0, 6, 0)
        self._host_layout.setSpacing(6)
        self._host_layout.addStretch(1)
        scroll.setWidget(self._host)
        outer.addWidget(scroll, 1)

        # Re-translate the header + rebuild rows (so per-row labels re-translate)
        # on language change.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

    def _retranslate(self) -> None:
        self._title.setText(i18n.t("conversations.title"))
        self._new_btn.setText("＋ " + i18n.t("conversations.new_chat"))
        self.refresh()

    def set_active(self, conv_id: str | None) -> None:
        self._active_id = conv_id
        self.refresh()

    def refresh(self) -> None:
        # Wipe & rebuild — the list is small so we don't bother diffing.
        while self._host_layout.count() > 1:
            item = self._host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        items = conversations.list_conversations()
        if not items:
            # An empty list is a valid state — never auto-create here. A new
            # conversation is created only when the user clicks "New chat"
            # or sends a first message.
            placeholder = QLabel(i18n.t("conversations.empty"))
            placeholder.setObjectName("panelEmpty")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # Stretch above + the host's trailing stretch below keep the
            # label vertically centered in the panel.
            self._host_layout.insertStretch(0, 1)
            self._host_layout.insertWidget(1, placeholder)
            return
        for i, conv in enumerate(items):
            row = _ConvRow(conv, is_active=(conv.get("id") == self._active_id))
            row.open_clicked.connect(self.open_conversation.emit)
            row.delete_clicked.connect(self.delete_conversation.emit)
            self._host_layout.insertWidget(i, row)


# ── Model panel ─────────────────────────────────────────────────────────────

class _ModelRow(QFrame):
    """One model entry — provider tag, label, select button."""

    selected = pyqtSignal(str)

    def __init__(self, model: dict, is_active: bool):
        super().__init__()
        self.setObjectName("modelRow")
        self.setProperty("active", is_active)
        self._mid = model["id"]

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(8)

        prov = QLabel(model.get("provider", "").upper()[:4])
        prov.setObjectName("modelProv")
        prov.setFixedWidth(48)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        name = QLabel(model.get("label") or model["id"])
        name.setObjectName("modelName")
        sub = QLabel(model.get("id", ""))
        sub.setObjectName("modelSub")
        col.addWidget(name)
        col.addWidget(sub)

        btn = QPushButton(
            "✓ " + i18n.t("models.active") if is_active else i18n.t("models.use")
        )
        btn.setObjectName("modelUse")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setEnabled(not is_active)
        btn.clicked.connect(lambda: self.selected.emit(self._mid))

        row.addWidget(prov)
        row.addLayout(col, 1)
        row.addWidget(btn)


class _InfoBadge(QLabel):
    """Small violet "!" circle (14px) showing a free-tier info tooltip.

    Used next to the provider name on the model panel grid cards and the
    settings provider cards for providers listed in ``FREETIER_INFO_KEYS``.
    The tooltip text is translated via i18n and re-translated in place when
    the UI language changes.
    """

    def __init__(self, i18n_key: str):
        super().__init__("!")
        self.setObjectName("infoBadge")
        self.setFixedSize(14, 14)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._i18n_key = i18n_key
        self._retranslate()
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

    def _retranslate(self) -> None:
        self.setToolTip(i18n.t(self._i18n_key))


class _ProviderGridCard(QFrame):
    """A provider tile in the model panel's first-level grid.

    Shows a colored dot indicator, the provider name and how many models it
    exposes. Clicking it drills into that provider's model list.
    """

    clicked = pyqtSignal(str)   # provider key

    def __init__(self, provider: str, color: str, model_count: int):
        super().__init__()
        self.setObjectName("provGridCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._provider = provider

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        # Single subtle violet dot for every provider (no per-provider colour).
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background: #c4b5f5; border-radius: 4px;")
        name = QLabel(PROVIDERS.get(provider, {}).get("name") or provider.title())
        name.setObjectName("provGridName")
        top.addWidget(dot)
        top.addWidget(name)
        # Free-tier "!" info badge for providers that have one (Google, Groq).
        freetier_key = FREETIER_INFO_KEYS.get(provider)
        if freetier_key:
            top.addWidget(_InfoBadge(freetier_key))
        top.addStretch()
        lay.addLayout(top)

        count = QLabel(
            i18n.t(
                "models.count_one" if model_count == 1 else "models.count_other",
                count=model_count,
            )
        )
        count.setObjectName("provGridCount")
        lay.addWidget(count)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._provider)
            event.accept()
            return
        super().mousePressEvent(event)


class _ModelPanel(QWidget):
    """Two-level model picker.

    First level is a grid of provider cards; clicking one drills into a list
    of that provider's models with "Use" buttons. A "← Back" button returns
    to the provider grid.
    """

    model_changed = pyqtSignal(str)

    PROVIDER_ORDER = ("anthropic", "openai", "deepseek", "google", "xai", "groq", "ollama")

    def __init__(self):
        super().__init__()
        self._current_model: str | None = None
        self._remote: list = []
        self._view: str | None = None   # None = provider grid, else provider key

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._back_btn = QPushButton("←  " + i18n.t("models.back"))
        self._back_btn.setObjectName("backBtn")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._show_grid)
        self._back_btn.hide()
        self._title = QLabel(i18n.t("models.title"))
        self._title.setObjectName("panelTitle")
        header.addWidget(self._back_btn)
        header.addWidget(self._title)
        header.addStretch()
        outer.addLayout(header)

        self._sub = QLabel(i18n.t("models.choose_provider_sub"))
        self._sub.setObjectName("panelSub")
        outer.addWidget(self._sub)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._host = QWidget()
        # QScrollArea.setWidget() forces autoFillBackground(True) on the host,
        # which paints the default grey palette. Name it so the stylesheet can
        # force it back to transparent and keep the app's dark background.
        self._host.setObjectName("panelHost")
        self._host_layout = QVBoxLayout(self._host)
        self._host_layout.setContentsMargins(0, 0, 6, 0)
        self._host_layout.setSpacing(6)
        self._host_layout.addStretch(1)
        scroll.setWidget(self._host)
        outer.addWidget(scroll, 1)

        # Re-translate the back button + rebuild the current view (grid or model
        # list) so its header, rows and placeholders re-translate on language
        # change.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

    def _retranslate(self) -> None:
        self._back_btn.setText("←  " + i18n.t("models.back"))
        self.refresh()

    def set_remote_models(self, remote: list) -> None:
        self._remote = remote or []
        self.refresh()

    def set_current(self, model_id: str | None) -> None:
        self._current_model = model_id
        self.refresh()

    # ── Navigation ─────────────────────────────────────────────────────────

    def _show_grid(self) -> None:
        self._view = None
        self.refresh()

    def _show_provider(self, provider: str) -> None:
        self._view = provider
        self.refresh()

    def refresh(self) -> None:
        while self._host_layout.count() > 1:
            item = self._host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if self._view is None:
            self._build_grid()
        else:
            self._build_model_list(self._view)

    # ── Level 1: provider grid ──────────────────────────────────────────────

    def _build_grid(self) -> None:
        self._back_btn.hide()
        self._title.setText(i18n.t("models.title"))
        self._sub.setText(i18n.t("models.choose_provider_sub"))

        models = get_all_models(remote_models=self._remote)
        counts: dict[str, int] = {}
        for m in models:
            counts[m["provider"]] = counts.get(m["provider"], 0) + 1

        # Only show providers that are actually usable: local (Ollama) or
        # with a saved API key. Unconfigured providers stay reachable from
        # the Settings panel, where their key can be added.
        keys = load_api_keys()
        configured = [
            p for p in self.PROVIDER_ORDER
            if p == "ollama" or keys.get(p)
        ]

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        cols = 2
        for i, provider in enumerate(configured):
            card = _ProviderGridCard(
                provider,
                PROVIDER_COLORS.get(provider, "#c4b5f5"),
                counts.get(provider, 0),
            )
            card.clicked.connect(self._show_provider)
            grid.addWidget(card, i // cols, i % cols)
        self._host_layout.insertWidget(0, grid_host)

    # ── Level 2: a provider's model list ────────────────────────────────────

    def _build_model_list(self, provider: str) -> None:
        self._back_btn.show()
        self._title.setText(
            PROVIDERS.get(provider, {}).get("name") or provider.title()
        )
        self._sub.setText(i18n.t("models.pick_model_sub"))

        models = [
            m for m in get_all_models(remote_models=self._remote)
            if m["provider"] == provider
        ]
        models.sort(key=lambda m: m.get("label", ""))

        if not models:
            empty = QLabel(i18n.t("models.none_for_provider"))
            empty.setObjectName("panelEmpty")
            self._host_layout.insertWidget(0, empty)
            return

        idx = 0
        for m in models:
            row = _ModelRow(m, is_active=(m["id"] == self._current_model))
            row.selected.connect(self.model_changed.emit)
            self._host_layout.insertWidget(idx, row)
            idx += 1


# ── Settings panel ──────────────────────────────────────────────────────────

class _ProviderCard(QFrame):
    """One AI provider as a compact configuration card.

    A "Get API key" row at the top opens the provider's official key page in
    the system browser. Beneath it, a row holds the colored status dot, the
    provider name, an API-key input and a Save button (which flashes green
    once the key is persisted). Every provider authenticates with its API
    key only — there is no OAuth.
    """

    saved = pyqtSignal(str)            # provider

    def __init__(self, provider: str):
        super().__init__()
        self.setObjectName("providerCard")
        self._provider = provider

        cfg = PROVIDERS[provider]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # "Get API key" row: label + Browse button that opens the provider's
        # official key page in the system browser.
        key_url = PROVIDER_KEY_URLS.get(provider)
        if key_url:
            get_row = QHBoxLayout()
            get_row.setSpacing(8)
            self._get_key_label = QLabel(i18n.t("provider.get_key"))
            self._get_key_label.setObjectName("providerSub")
            self._browse_btn = QPushButton(i18n.t("provider.browse"))
            self._browse_btn.setObjectName("ghostBtn")
            self._browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._browse_btn.clicked.connect(self._on_browse)
            get_row.addWidget(self._get_key_label)
            get_row.addStretch()
            get_row.addWidget(self._browse_btn)
            layout.addLayout(get_row)
        else:
            self._get_key_label = None
            self._browse_btn = None

        # Compact row: dot + name + key input + Save
        key_row = QHBoxLayout()
        key_row.setSpacing(8)

        self._status_dot = QLabel()
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setFixedSize(8, 8)
        self._status_dot.setProperty("state", "stopped")

        name = QLabel(cfg["name"])
        name.setObjectName("providerName")
        name.setMinimumWidth(72)

        self._key_input = QLineEdit()
        self._key_input.setObjectName("keyInput")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText(
            i18n.t("provider.key_placeholder", name=cfg["name"])
        )
        self._save_btn = QPushButton(i18n.t("provider.save"))
        self._save_btn.setObjectName("saveBtn")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)

        key_row.addWidget(self._status_dot)
        key_row.addWidget(name)
        # Free-tier "!" info badge for providers that have one (Google, Groq).
        freetier_key = FREETIER_INFO_KEYS.get(provider)
        if freetier_key:
            key_row.addWidget(_InfoBadge(freetier_key))
        key_row.addWidget(self._key_input, 1)
        key_row.addWidget(self._save_btn)
        layout.addLayout(key_row)

        # Re-translate in place when the UI language changes.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

    def _retranslate(self) -> None:
        cfg = PROVIDERS[self._provider]
        if self._get_key_label is not None:
            self._get_key_label.setText(i18n.t("provider.get_key"))
        if self._browse_btn is not None:
            self._browse_btn.setText(i18n.t("provider.browse"))
        self._key_input.setPlaceholderText(
            i18n.t("provider.key_placeholder", name=cfg["name"])
        )
        # Don't clobber the transient "Saved ✓" flash.
        if not self._save_btn.property("saved"):
            self._save_btn.setText(i18n.t("provider.save"))

    def _on_browse(self) -> None:
        url = PROVIDER_KEY_URLS.get(self._provider)
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def set_key(self, key: str) -> None:
        self._key_input.setText(key or "")
        self._update_status_dot()

    def current_key(self) -> str:
        return self._key_input.text().strip()

    def _update_status_dot(self) -> None:
        connected = bool(self.current_key())
        self._status_dot.setProperty(
            "state", "running" if connected else "stopped"
        )
        self._status_dot.style().unpolish(self._status_dot)
        self._status_dot.style().polish(self._status_dot)

    def _on_save(self) -> None:
        key = self.current_key()
        if key:
            set_api_key(self._provider, key)
        else:
            set_api_key(self._provider, "")
        self._flash_saved()
        self._update_status_dot()
        self.saved.emit(self._provider)

    def _flash_saved(self) -> None:
        self._save_btn.setText(i18n.t("provider.saved"))
        self._save_btn.setProperty("saved", True)
        self._save_btn.style().unpolish(self._save_btn)
        self._save_btn.style().polish(self._save_btn)

        def restore():
            self._save_btn.setProperty("saved", False)
            self._save_btn.setText(i18n.t("provider.save"))
            self._save_btn.style().unpolish(self._save_btn)
            self._save_btn.style().polish(self._save_btn)

        QTimer.singleShot(1500, restore)


class _TokenDialog(QDialog):
    """Small modal that reveals a mobile token in clear text until closed.

    Used by both the "Show" and "Regenerate" actions in the Mobile Connection
    section. The token is shown in a read-only, selectable field with a Copy
    button; closing the dialog leaves the section's masked display untouched.
    """

    def __init__(self, parent, title: str, token: str):
        super().__init__(parent)
        self.setObjectName("tokenDialog")
        # The parent popup is WindowStaysOnTopHint|Tool — a modal child that is
        # not itself always-on-top can paint BEHIND it on some X11/Wayland WMs,
        # which reads as a frozen UI (modal blocks input but the dialog hides).
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
        lay.setSpacing(12)

        title_label = QLabel(title)
        title_label.setObjectName("dialogTitle")
        lay.addWidget(title_label)

        self._field = QLineEdit(token)
        self._field.setObjectName("keyInput")
        self._field.setReadOnly(True)
        self._field.setMinimumWidth(300)
        self._field.setCursorPosition(0)
        lay.addWidget(self._field)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self._copy_btn = QPushButton(i18n.t("settings.copy"))
        self._copy_btn.setObjectName("ghostBtn")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.clicked.connect(self._on_copy)
        close_btn = QPushButton(i18n.t("settings.close"))
        close_btn.setObjectName("primaryBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._copy_btn)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._field.text())
        self._copy_btn.setText(i18n.t("settings.copied"))
        QTimer.singleShot(
            1500, lambda: self._copy_btn.setText(i18n.t("settings.copy"))
        )

    def showEvent(self, event):
        super().showEvent(event)
        # Center over the parent window (the popup is frameless/always-on-top).
        parent = self.parent()
        if parent is not None:
            geo = parent.window().frameGeometry()
            self.move(geo.center() - self.rect().center())
        # Guarantee the dialog surfaces above the always-on-top parent.
        self.raise_()
        self.activateWindow()


class _PopoverDialog(QDialog):
    """Frameless, dark, always-on-top modal used as a settings popover/flyout.

    Shares the premium ``dialogCard`` styling with :class:`_TokenDialog`.
    Subclasses fill ``self._card_layout``; this base handles the window flags,
    translucent background and the "surface above the always-on-top parent"
    placement (anchored under a trigger widget when one is given, else centred).
    """

    def __init__(self, parent, anchor=None):
        super().__init__(parent)
        self._anchor = anchor
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
        self._card_layout = QVBoxLayout(card)
        self._card_layout.setContentsMargins(18, 16, 18, 16)
        self._card_layout.setSpacing(10)

    def showEvent(self, event):
        super().showEvent(event)
        anchor = self._anchor
        placed = False
        try:
            if anchor is not None and anchor.isVisible():
                # Flush the popover just under its trigger, left edges aligned,
                # nudged on-screen if it would spill past the screen edge.
                gp = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
                screen = QGuiApplication.screenAt(gp) or QGuiApplication.primaryScreen()
                avail = screen.availableGeometry()
                x = min(gp.x(), avail.right() - self.width() - 8)
                x = max(avail.left() + 8, x)
                y = min(gp.y(), avail.bottom() - self.height() - 8)
                self.move(x, max(avail.top() + 8, y))
                placed = True
        except Exception:
            placed = False
        if not placed:
            parent = self.parent()
            if parent is not None:
                geo = parent.window().frameGeometry()
                self.move(geo.center() - self.rect().center())
        self.raise_()
        self.activateWindow()


class _ApiKeyFallbackDialog(_PopoverDialog):
    """Popover to edit a provider's ordered API-key fallback hierarchy.

    Row 1 is the primary key; the rest are tried in order on a 429/auth error
    (see :meth:`OSIAAgent._activate_next_api_key`). Keys are masked. On close
    the cleaned list is persisted to ``api_key_fallbacks[provider]`` in
    settings.json (the provider entry is dropped when no key remains).
    """

    def __init__(self, parent, provider: str, provider_name: str,
                 keys: list, anchor=None):
        super().__init__(parent, anchor=anchor)
        self.setObjectName("apiKeyDialog")
        self._provider = provider
        self._keys = [str(k) for k in keys] if keys else [""]

        lay = self._card_layout
        title = QLabel(i18n.t("api_keys.dialog_title", name=provider_name))
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        sub = QLabel(i18n.t("api_keys.dialog_sub"))
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)
        sub.setMaximumWidth(360)
        lay.addWidget(sub)

        host = QWidget()
        self._rows_host = QVBoxLayout(host)
        self._rows_host.setContentsMargins(0, 0, 0, 0)
        self._rows_host.setSpacing(6)
        lay.addWidget(host)

        add_btn = QPushButton("＋ " + i18n.t("api_keys.add"))
        add_btn.setObjectName("ghostBtn")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._on_add)
        lay.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignLeft)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(i18n.t("settings.close"))
        close_btn.setObjectName("primaryBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._render_rows()

    def _render_rows(self) -> None:
        while self._rows_host.count():
            item = self._rows_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, key in enumerate(self._keys):
            self._rows_host.addWidget(self._build_row(i, key))

    def _build_row(self, index: int, key: str) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        label = QLabel(i18n.t("api_keys.primary") if index == 0
                       else i18n.t("api_keys.fallback_n", n=index))
        label.setObjectName("fieldLabel")
        label.setMinimumWidth(74)
        label.setWordWrap(True)
        inp = QLineEdit(key)
        inp.setObjectName("keyInput")
        inp.setEchoMode(QLineEdit.EchoMode.Password)
        inp.setMinimumWidth(220)
        inp.setPlaceholderText(i18n.t("api_keys.key_placeholder"))
        inp.textChanged.connect(lambda txt, i=index: self._on_text(i, txt))
        rm = QPushButton("✕")
        rm.setObjectName("convDel")
        rm.setFixedSize(28, 28)
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.clicked.connect(lambda _=False, i=index: self._on_remove(i))
        # The list must always keep at least one slot (the primary).
        rm.setEnabled(len(self._keys) > 1)
        h.addWidget(label)
        h.addWidget(inp, 1)
        h.addWidget(rm)
        return row

    def _on_text(self, index: int, txt: str) -> None:
        if 0 <= index < len(self._keys):
            self._keys[index] = txt

    def _on_add(self) -> None:
        self._keys.append("")
        self._render_rows()

    def _on_remove(self, index: int) -> None:
        if 0 <= index < len(self._keys) and len(self._keys) > 1:
            self._keys.pop(index)
            self._render_rows()

    def _persist(self) -> None:
        cleaned = [k.strip() for k in self._keys if k and k.strip()]
        settings = load_settings()
        fallbacks = dict(settings.get("api_key_fallbacks", {}) or {})
        if cleaned:
            fallbacks[self._provider] = cleaned
        else:
            fallbacks.pop(self._provider, None)
        set_setting("api_key_fallbacks", fallbacks)

    def accept(self):
        self._persist()
        super().accept()


class _CronConversationDialog(_PopoverDialog):
    """Popover to link a scheduled task to an existing conversation.

    Lists a "None" option plus every stored conversation by title. Choosing a
    row sets :attr:`result_id` and accepts; Esc cancels (leaving it unchanged).
    """

    def __init__(self, parent, current_id, anchor=None):
        super().__init__(parent, anchor=anchor)
        self.setObjectName("convPickerDialog")
        self.result_id = current_id

        lay = self._card_layout
        title = QLabel(i18n.t("cron.conversation_dialog_title"))
        title.setObjectName("dialogTitle")
        lay.addWidget(title)
        sub = QLabel(i18n.t("cron.conversation_dialog_sub"))
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)
        sub.setMaximumWidth(340)
        lay.addWidget(sub)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("convScroll")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(280)
        scroll.setMinimumWidth(320)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)

        bl.addWidget(self._option_btn(
            i18n.t("cron.conversation_none_option"), None, current_id))
        convs = conversations.list_conversations()
        if not convs:
            empty = QLabel(i18n.t("cron.conversation_empty"))
            empty.setObjectName("providerSub")
            empty.setWordWrap(True)
            bl.addWidget(empty)
        for c in convs:
            cid = c.get("id")
            title_txt = c.get("title") or i18n.t("cron.conversation_none")
            bl.addWidget(self._option_btn(title_txt, cid, current_id))
        bl.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll)

    def _option_btn(self, text: str, conv_id, current_id) -> QPushButton:
        marker = "✓  " if conv_id == current_id else "     "
        btn = QPushButton(marker + text)
        btn.setObjectName("convPick")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _=False, cid=conv_id: self._choose(cid))
        return btn

    def _choose(self, conv_id) -> None:
        self.result_id = conv_id
        self.accept()


class _SettingsPanel(QWidget):
    """Settings page — organised into three clearly separated provider
    sections plus mobile-token management.

    * Section 1 — AI Providers (text / chat model API keys)
    * Section 2 — Voice Providers (text-to-speech)
    * Section 3 — Whisper Providers (speech-to-text)
    * Mobile Connection — reveal / regenerate the mobile pairing token

    Around them sit the original language selector and startup systemd
    services.
    """

    api_keys_saved = pyqtSignal(dict)
    # Emitted when the "Show the floating capsule" toggle changes (WOR-22).
    # The host (osia.main.wake_mode) connects it to the capsule's visibility.
    capsule_visibility_changed = pyqtSignal(bool)

    # Order AI provider cards appear in Section 1 (Ollama is keyless and shown
    # last as an info-only row).
    _AI_PROVIDER_ORDER = ("anthropic", "openai", "google", "deepseek", "xai", "groq")

    # Active TTS provider options: (settings value, combo label / i18n key).
    _TTS_PROVIDERS = (
        ("local",  "tts.local_option"),
        ("openai", None),               # proper noun — shown verbatim
        ("google", None),
    )
    _TTS_PROVIDER_NAMES = {"openai": "OpenAI", "google": "Google Cloud"}

    # STT model sub-options per provider: (combo label, settings value).
    _STT_LOCAL_MODELS = (("small", "small"), ("medium", "medium"))
    _STT_GROQ_MODELS = (
        ("large-v3-turbo", "whisper-large-v3-turbo"),
        ("large-v3", "whisper-large-v3"),
    )

    # ── Live API (Bloc 4) ──────────────────────────────────────────────────
    # Order Live API providers appear in the selector.
    _LIVEAPI_PROVIDER_ORDER = ("google", "openai", "claude")
    # Live API provider value -> verbatim display name (proper nouns, untranslated).
    _LIVEAPI_PROVIDER_NAMES = {
        "google": "Google (Gemini Live)",
        "openai": "OpenAI Realtime API",
        "claude": "Claude Live API",
    }
    # Live API provider value -> AI-provider key store entry it reuses (so the
    # key set in "AI Providers" is shared; no duplicate storage).
    _LIVEAPI_KEY_PROVIDER = {
        "google": "google",
        "openai": "openai",
        "claude": "anthropic",
    }
    # Live API provider value -> selectable live/realtime models: (label, id).
    _LIVEAPI_MODELS = {
        "google": (
            ("gemini-3.1-flash-live", "gemini-3.1-flash-live"),
            ("gemini-2.5-flash-live", "gemini-2.5-flash-live"),
        ),
        "openai": (
            ("gpt-realtime", "gpt-realtime"),
            ("gpt-4o-realtime", "gpt-4o-realtime-preview"),
            ("gpt-4o-mini-realtime", "gpt-4o-mini-realtime-preview"),
        ),
        "claude": (
            ("Claude Opus 4.8 (live)", "claude-opus-4-8-live"),
            ("Claude Sonnet 4.6 (live)", "claude-sonnet-4-6-live"),
        ),
    }
    # Live API provider value -> official live/realtime documentation page,
    # opened by the "Naviguer" button.
    _LIVEAPI_DOCS_URLS = {
        "google": "https://ai.google.dev/gemini-api/docs/live",
        "openai": "https://platform.openai.com/docs/guides/realtime",
        "claude": "https://docs.anthropic.com/en/docs/build-with-claude/streaming",
    }

    def __init__(self):
        super().__init__()

        self._provider_cards: dict[str, _ProviderCard] = {}
        self._tts_cards: dict[str, _ProviderCard] = {}
        self._liveapi_cards: dict[str, _ProviderCard] = {}
        self._mobile_token: str = ""
        self._startup_refresh_guard = False
        self._startup_rows: list[tuple[str, QLabel, QCheckBox]] = []
        # Guards the autostart toggle against re-entrant stateChanged while it is
        # being reverted (e.g. on an unsupported platform). See WOR-22.
        self._autostart_guard = False
        self._drag_pos: QPoint | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        self._settings_title = QLabel(i18n.t("settings.title"))
        self._settings_title.setObjectName("panelTitle")
        self._settings_title.setWordWrap(True)
        header.addWidget(self._settings_title)
        header.addStretch()
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        host = QWidget()
        # Same as the other panels: keep the scroll host transparent so the
        # settings page shows the app's dark background, not the grey palette.
        host.setObjectName("panelHost")
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(14)

        v.addWidget(self._build_language_section())
        v.addWidget(self._build_capsule_section())          # Capsule + autostart
        v.addWidget(self._build_ai_providers_section())     # Section 1
        v.addWidget(self._build_voice_section())            # Section 2 (TTS)
        v.addWidget(self._build_wake_section())             # Wake-word greeting
        v.addWidget(self._build_stt_section())              # Section 3 (STT)
        v.addWidget(self._build_liveapi_section())          # Section 4 (Live API)
        v.addWidget(self._build_ai_hierarchy_section())     # AI Task Hierarchy
        v.addWidget(self._build_cron_section())             # Scheduled Tasks (Bloc 5)
        v.addWidget(self._build_heartbeat_section())        # Heartbeat (Bloc 5)
        v.addWidget(self._build_apikeys_section())          # API Key Fallback
        v.addWidget(self._build_mobile_section())           # Mobile Connection
        v.addWidget(self._build_startup_section())
        v.addStretch(1)

        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        # Re-translate this panel's own section chrome on language change.
        i18n.add_listener(self._retranslate_sections)
        self.destroyed.connect(
            lambda: i18n.remove_listener(self._retranslate_sections)
        )

        self._apply_style()
        self._load_state()

        # Refresh startup status while visible.
        self._startup_timer = QTimer(self)
        self._startup_timer.setInterval(5000)
        self._startup_timer.timeout.connect(self._refresh_startup_status)

    # ── Section builders ──────────────────────────────────────────────────

    def _build_language_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._language_title = QLabel(i18n.t("settings.language"))
        self._language_title.setObjectName("sectionTitle")
        self._language_title.setWordWrap(True)
        self._language_sub = QLabel(i18n.t("settings.language_sub"))
        self._language_sub.setObjectName("sectionSub")
        self._language_sub.setWordWrap(True)
        l.addWidget(self._language_title)
        l.addWidget(self._language_sub)

        self._language_combo = QComboBox()
        self._language_combo.setObjectName("voiceCombo")
        for code, name in i18n.available_languages().items():
            self._language_combo.addItem(name, code)
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)
        l.addWidget(self._language_combo)

        # Time format (12h / 24h) — applies to every wall-clock time OSIA shows
        # (conversation timestamps, cron daily schedules, heartbeat alerts).
        self._timefmt_label = QLabel(i18n.t("settings.time_format"))
        self._timefmt_label.setObjectName("sectionSub")
        self._timefmt_label.setWordWrap(True)
        l.addWidget(self._timefmt_label)
        self._timefmt_combo = QComboBox()
        self._timefmt_combo.setObjectName("voiceCombo")
        self._timefmt_combo.addItem(i18n.t("settings.time_format_24h"), "24h")
        self._timefmt_combo.addItem(i18n.t("settings.time_format_12h"), "12h")
        self._timefmt_combo.currentIndexChanged.connect(self._on_time_format_changed)
        l.addWidget(self._timefmt_combo)

        # Keep this section's own labels translated on language change.
        i18n.add_listener(self._retranslate_language_section)
        self.destroyed.connect(
            lambda: i18n.remove_listener(self._retranslate_language_section)
        )
        return wrap

    def _retranslate_language_section(self) -> None:
        self._language_title.setText(i18n.t("settings.language"))
        self._language_sub.setText(i18n.t("settings.language_sub"))
        self._timefmt_label.setText(i18n.t("settings.time_format"))
        self._timefmt_combo.setItemText(0, i18n.t("settings.time_format_24h"))
        self._timefmt_combo.setItemText(1, i18n.t("settings.time_format_12h"))

    def _on_language_changed(self, _idx: int) -> None:
        code = self._language_combo.currentData()
        if code:
            i18n.set_language(code)

    def _on_time_format_changed(self, _idx: int) -> None:
        fmt = self._timefmt_combo.currentData()
        if not fmt:
            return
        set_setting("time_format", fmt)
        # Live-refresh the times shown in this panel (cron daily schedules);
        # the conversation list re-reads the setting when it is next opened.
        self._render_cron()

    def _build_capsule_section(self) -> QFrame:
        """Floating-capsule controls (WOR-22): show/hide it + launch at login."""
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._capsule_title = QLabel(i18n.t("settings.capsule"))
        self._capsule_title.setObjectName("sectionTitle")
        self._capsule_title.setWordWrap(True)
        l.addWidget(self._capsule_title)

        # Toggle: show / hide the floating capsule window.
        show_row = QHBoxLayout()
        self._show_capsule_label = QLabel(i18n.t("settings.show_capsule"))
        self._show_capsule_label.setObjectName("providerName")
        self._show_capsule_label.setWordWrap(True)
        self._show_capsule_toggle = QCheckBox()
        self._show_capsule_toggle.setObjectName("smartToggle")
        self._show_capsule_toggle.stateChanged.connect(
            self._on_show_capsule_changed
        )
        show_row.addWidget(self._show_capsule_label)
        show_row.addStretch()
        show_row.addWidget(self._show_capsule_toggle)
        l.addLayout(show_row)

        # Toggle: launch OSIA (wake mode) automatically at login.
        auto_row = QHBoxLayout()
        self._autostart_label = QLabel(i18n.t("settings.autostart_gui"))
        self._autostart_label.setObjectName("providerName")
        self._autostart_label.setWordWrap(True)
        self._autostart_toggle = QCheckBox()
        self._autostart_toggle.setObjectName("smartToggle")
        self._autostart_toggle.stateChanged.connect(self._on_autostart_changed)
        auto_row.addWidget(self._autostart_label)
        auto_row.addStretch()
        auto_row.addWidget(self._autostart_toggle)
        l.addLayout(auto_row)

        return wrap

    def _on_show_capsule_changed(self, _state) -> None:
        checked = self._show_capsule_toggle.isChecked()
        set_setting("show_capsule", checked)
        self.capsule_visibility_changed.emit(checked)

    def _on_autostart_changed(self, _state) -> None:
        if self._autostart_guard:
            return
        checked = self._autostart_toggle.isChecked()
        ok = set_autostart(checked)
        if not ok:
            # Unsupported platform (Windows/Mac stub) or a write failure —
            # revert the toggle so it keeps reflecting the real state.
            self._autostart_guard = True
            self._autostart_toggle.setChecked(not checked)
            self._autostart_guard = False

    def _build_startup_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._startup_title = QLabel(i18n.t("settings.startup"))
        self._startup_title.setObjectName("sectionTitle")
        self._startup_title.setWordWrap(True)
        self._startup_sub = QLabel(i18n.t("settings.startup_sub"))
        self._startup_sub.setObjectName("sectionSub")
        self._startup_sub.setWordWrap(True)
        l.addWidget(self._startup_title)
        l.addWidget(self._startup_sub)

        # (unit, i18n key) — the label widget + key are tracked in
        # self._startup_labels so they re-translate on language change.
        self._startup_labels: list[tuple[QLabel, str]] = []
        rows = [
            (STARTUP_SERVICES[0], "settings.startup_mobile_server"),
            (STARTUP_SERVICES[1], "settings.startup_cloudflare"),
        ]
        for unit, label_key in rows:
            row = QHBoxLayout()
            label = QLabel(i18n.t(label_key))
            label.setObjectName("startupLabel")
            label.setWordWrap(True)
            self._startup_labels.append((label, label_key))
            dot = QLabel()
            dot.setObjectName("statusDot")
            dot.setFixedSize(10, 10)
            dot.setProperty("state", "stopped")
            toggle = QCheckBox()
            toggle.setObjectName("smartToggle")
            toggle.stateChanged.connect(
                lambda _s, u=unit, t=toggle: self._on_startup_toggle_changed(u, t)
            )
            row.addWidget(label)
            row.addStretch()
            row.addWidget(dot)
            row.addWidget(toggle)
            l.addLayout(row)
            self._startup_rows.append((unit, dot, toggle))

        return wrap

    # ── Section 2: voice providers (text-to-speech) ────────────────────────

    def _build_voice_section(self) -> QFrame:
        from osia.voice.tts import (
            OPENAI_VOICES, GEMINI_VOICES, PROVIDER_DEFAULT_VOICE,
        )
        self._openai_voices = list(OPENAI_VOICES)
        # Gemini prebuilt voices are multilingual (70+ languages), so they're
        # listed by name with no per-language split. Default is Kore.
        self._google_voices = [(v, v) for v in GEMINI_VOICES]
        self._provider_default_voice = dict(PROVIDER_DEFAULT_VOICE)

        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._voice_title = QLabel(i18n.t("settings.voice_providers"))
        self._voice_title.setObjectName("sectionTitle")
        self._voice_title.setWordWrap(True)
        self._voice_sub = QLabel(i18n.t("settings.voice_providers_sub"))
        self._voice_sub.setObjectName("sectionSub")
        self._voice_sub.setWordWrap(True)
        l.addWidget(self._voice_title)
        l.addWidget(self._voice_sub)

        # Active provider selector.
        prov_row = QHBoxLayout()
        self._voice_prov_label = QLabel(i18n.t("settings.active_provider"))
        self._voice_prov_label.setObjectName("fieldLabel")
        self._voice_prov_label.setMinimumWidth(96)
        self._voice_prov_label.setWordWrap(True)
        self._tts_provider_combo = QComboBox()
        self._tts_provider_combo.setObjectName("voiceCombo")
        for key, i18n_key in self._TTS_PROVIDERS:
            label = i18n.t(i18n_key) if i18n_key else self._TTS_PROVIDER_NAMES[key]
            self._tts_provider_combo.addItem(label, key)
        self._tts_provider_combo.currentIndexChanged.connect(
            self._on_tts_provider_changed
        )
        prov_row.addWidget(self._voice_prov_label)
        prov_row.addWidget(self._tts_provider_combo, 1)
        l.addLayout(prov_row)

        # Voice selector (hidden for the local/Kokoro provider).
        self._tts_voice_row = QWidget()
        voice_row = QHBoxLayout(self._tts_voice_row)
        voice_row.setContentsMargins(0, 0, 0, 0)
        self._voice_voice_label = QLabel(i18n.t("settings.voice"))
        self._voice_voice_label.setObjectName("fieldLabel")
        self._voice_voice_label.setMinimumWidth(96)
        self._voice_voice_label.setWordWrap(True)
        self._tts_voice_combo = QComboBox()
        self._tts_voice_combo.setObjectName("voiceCombo")
        self._tts_voice_combo.currentIndexChanged.connect(self._on_tts_voice_changed)
        voice_row.addWidget(self._voice_voice_label)
        voice_row.addWidget(self._tts_voice_combo, 1)
        l.addWidget(self._tts_voice_row)

        # Kokoro — local, free, keyless info row. Shown only when the local
        # provider is active (see _populate_voice_combo).
        self._kokoro_row = QFrame()
        self._kokoro_row.setObjectName("providerCard")
        kr = QVBoxLayout(self._kokoro_row)
        kr.setContentsMargins(12, 10, 12, 10)
        krh = QHBoxLayout()
        krh.setSpacing(8)
        kokoro_name = QLabel("Kokoro")
        kokoro_name.setObjectName("providerName")
        kokoro_name.setWordWrap(True)
        self._kokoro_tag = QLabel(i18n.t("tts.kokoro_desc"))
        self._kokoro_tag.setObjectName("providerSub")
        self._kokoro_tag.setWordWrap(True)
        krh.addWidget(kokoro_name)
        krh.addStretch()
        krh.addWidget(self._kokoro_tag)
        kr.addLayout(krh)
        l.addWidget(self._kokoro_row)

        # OpenAI / Google cloud TTS — reuse the AI provider key (same store),
        # so a key saved here also unlocks chat models and vice-versa. Only the
        # active provider's card is shown (see _populate_voice_combo).
        for provider in ("openai", "google"):
            card = _ProviderCard(provider)
            card.saved.connect(self._on_provider_saved)
            l.addWidget(card)
            self._tts_cards[provider] = card

        return wrap

    # ── Wake-word greeting response ────────────────────────────────────────
    # A master toggle + a single text field. When enabled, OSIA speaks the
    # text right after the wake word and before listening (see
    # osia.main.wake_mode.handle_wake). An empty field auto-fills with the
    # active language's default greeting.

    def _build_wake_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._wake_title = QLabel(i18n.t("settings.wake_greeting"))
        self._wake_title.setObjectName("sectionTitle")
        self._wake_title.setWordWrap(True)
        self._wake_sub = QLabel(i18n.t("settings.wake_greeting_sub"))
        self._wake_sub.setObjectName("sectionSub")
        self._wake_sub.setWordWrap(True)
        l.addWidget(self._wake_title)
        l.addWidget(self._wake_sub)

        enable_row = QHBoxLayout()
        self._wake_enabled_label = QLabel(i18n.t("wake.greeting_enabled"))
        self._wake_enabled_label.setObjectName("providerName")
        self._wake_enabled_label.setWordWrap(True)
        self._wake_enabled_toggle = QCheckBox()
        self._wake_enabled_toggle.setObjectName("smartToggle")
        self._wake_enabled_toggle.stateChanged.connect(self._on_wake_enabled_changed)
        enable_row.addWidget(self._wake_enabled_label)
        enable_row.addStretch()
        enable_row.addWidget(self._wake_enabled_toggle)
        l.addLayout(enable_row)

        self._wake_text_input = QLineEdit()
        self._wake_text_input.setObjectName("keyInput")
        self._wake_text_input.setPlaceholderText(i18n.t("wake.greeting_placeholder"))
        self._wake_text_input.editingFinished.connect(self._on_wake_text_changed)
        l.addWidget(self._wake_text_input)
        return wrap

    def _on_wake_enabled_changed(self, _state) -> None:
        enabled = self._wake_enabled_toggle.isChecked()
        set_setting("wake_greeting_enabled", enabled)
        if enabled:
            # Persist whatever is shown (the auto-filled language default when
            # the user hasn't typed their own) so the spoken greeting is never
            # silently empty the first time the toggle is switched on.
            text = self._wake_text_input.text().strip() or i18n.t("wake.greeting_default")
            self._wake_text_input.setText(text)
            set_setting("wake_greeting_text", text)

    def _on_wake_text_changed(self) -> None:
        set_setting("wake_greeting_text", self._wake_text_input.text().strip())

    # ── Section 3: Whisper providers (speech-to-text) ──────────────────────

    def _build_stt_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._stt_title = QLabel(i18n.t("settings.whisper_providers"))
        self._stt_title.setObjectName("sectionTitle")
        self._stt_title.setWordWrap(True)
        self._stt_sub = QLabel(i18n.t("settings.whisper_providers_sub"))
        self._stt_sub.setObjectName("sectionSub")
        self._stt_sub.setWordWrap(True)
        l.addWidget(self._stt_title)
        l.addWidget(self._stt_sub)

        # Active provider selector: faster-whisper (local) or Groq Whisper.
        prov_row = QHBoxLayout()
        self._stt_prov_label = QLabel(i18n.t("settings.active_provider"))
        self._stt_prov_label.setObjectName("fieldLabel")
        self._stt_prov_label.setMinimumWidth(96)
        self._stt_prov_label.setWordWrap(True)
        self._stt_provider_combo = QComboBox()
        self._stt_provider_combo.setObjectName("voiceCombo")
        self._stt_provider_combo.addItem(i18n.t("stt.local_name"), "local")
        self._stt_provider_combo.addItem(i18n.t("stt.groq_name"), "groq")
        self._stt_provider_combo.currentIndexChanged.connect(
            self._on_stt_provider_changed
        )
        prov_row.addWidget(self._stt_prov_label)
        prov_row.addWidget(self._stt_provider_combo, 1)
        # Free-tier "!" badge — only meaningful for Groq; toggled with provider.
        self._stt_groq_badge = _InfoBadge("groq_freetier_info")
        prov_row.addWidget(self._stt_groq_badge)
        l.addLayout(prov_row)

        # Model sub-options (small/medium for local, large-v3* for Groq).
        model_row = QHBoxLayout()
        self._stt_model_label = QLabel(i18n.t("settings.model"))
        self._stt_model_label.setObjectName("fieldLabel")
        self._stt_model_label.setMinimumWidth(96)
        self._stt_model_label.setWordWrap(True)
        self._stt_model_combo = QComboBox()
        self._stt_model_combo.setObjectName("voiceCombo")
        self._stt_model_combo.currentIndexChanged.connect(
            self._on_stt_model_changed
        )
        model_row.addWidget(self._stt_model_label)
        model_row.addWidget(self._stt_model_combo, 1)
        l.addLayout(model_row)

        # Fallback provider selector — the engine OSIA uses if the primary STT
        # fails (error / no key / timeout). "None" disables fallback entirely.
        fb_row = QHBoxLayout()
        self._stt_fallback_label = QLabel(i18n.t("settings.fallback_provider"))
        self._stt_fallback_label.setObjectName("fieldLabel")
        self._stt_fallback_label.setMinimumWidth(96)
        self._stt_fallback_label.setWordWrap(True)
        self._stt_fallback_combo = QComboBox()
        self._stt_fallback_combo.setObjectName("voiceCombo")
        self._stt_fallback_combo.addItem(i18n.t("stt.local_name"), "local")
        self._stt_fallback_combo.addItem(i18n.t("stt.groq_name"), "groq")
        self._stt_fallback_combo.addItem(i18n.t("stt.fallback_none"), "none")
        self._stt_fallback_combo.currentIndexChanged.connect(
            self._on_stt_fallback_changed
        )
        fb_row.addWidget(self._stt_fallback_label)
        fb_row.addWidget(self._stt_fallback_combo, 1)
        l.addLayout(fb_row)

        # Fallback model sub-option — Groq model ids when the fallback is Groq,
        # local faster-whisper sizes when local. Wrapped in a widget so the
        # whole row can be hidden when the fallback is "None".
        self._stt_fallback_model_row = QWidget()
        fb_model_row = QHBoxLayout(self._stt_fallback_model_row)
        fb_model_row.setContentsMargins(0, 0, 0, 0)
        self._stt_fallback_model_label = QLabel(i18n.t("settings.model"))
        self._stt_fallback_model_label.setObjectName("fieldLabel")
        self._stt_fallback_model_label.setMinimumWidth(96)
        self._stt_fallback_model_label.setWordWrap(True)
        self._stt_fallback_model_combo = QComboBox()
        self._stt_fallback_model_combo.setObjectName("voiceCombo")
        self._stt_fallback_model_combo.currentIndexChanged.connect(
            self._on_stt_fallback_model_changed
        )
        fb_model_row.addWidget(self._stt_fallback_model_label)
        fb_model_row.addWidget(self._stt_fallback_model_combo, 1)
        l.addWidget(self._stt_fallback_model_row)

        # Hint that Groq STT reuses the Groq API key from Section 1.
        self._stt_groq_hint = QLabel(i18n.t("stt.groq_hint"))
        self._stt_groq_hint.setObjectName("providerSub")
        self._stt_groq_hint.setWordWrap(True)
        l.addWidget(self._stt_groq_hint)

        return wrap

    # ── Section 4: Live API (voice-to-voice) (Bloc 4) ──────────────────────
    # When enabled, OSIA streams audio to a provider's realtime/live endpoint
    # and bypasses local STT (Whisper) + TTS entirely (see osia.main.wake_mode
    # and osia.voice.live_api). The whole configuration body stays collapsed
    # until the master toggle is switched on. The API key is reused from the
    # matching AI provider card — no duplicate storage.

    def _build_liveapi_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._liveapi_title = QLabel(i18n.t("settings.liveapi"))
        self._liveapi_title.setObjectName("sectionTitle")
        self._liveapi_title.setWordWrap(True)
        self._liveapi_sub = QLabel(i18n.t("settings.liveapi_sub"))
        self._liveapi_sub.setObjectName("sectionSub")
        self._liveapi_sub.setWordWrap(True)
        l.addWidget(self._liveapi_title)
        l.addWidget(self._liveapi_sub)

        # Master toggle — the configuration body below stays hidden until ON.
        enable_row = QHBoxLayout()
        self._liveapi_enabled_label = QLabel(i18n.t("liveapi.enabled"))
        self._liveapi_enabled_label.setObjectName("providerName")
        self._liveapi_enabled_label.setWordWrap(True)
        self._liveapi_enabled_toggle = QCheckBox()
        self._liveapi_enabled_toggle.setObjectName("smartToggle")
        self._liveapi_enabled_toggle.stateChanged.connect(
            self._on_liveapi_enabled_changed
        )
        enable_row.addWidget(self._liveapi_enabled_label)
        enable_row.addStretch()
        enable_row.addWidget(self._liveapi_enabled_toggle)
        l.addLayout(enable_row)

        # Collapsible body — shown only when Live API is enabled.
        self._liveapi_body = QWidget()
        body = QVBoxLayout(self._liveapi_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        # Provider selector (Google / OpenAI / Claude).
        prov_row = QHBoxLayout()
        self._liveapi_prov_label = QLabel(i18n.t("settings.active_provider"))
        self._liveapi_prov_label.setObjectName("fieldLabel")
        self._liveapi_prov_label.setMinimumWidth(96)
        self._liveapi_prov_label.setWordWrap(True)
        self._liveapi_provider_combo = QComboBox()
        self._liveapi_provider_combo.setObjectName("voiceCombo")
        for key in self._LIVEAPI_PROVIDER_ORDER:
            self._liveapi_provider_combo.addItem(
                self._LIVEAPI_PROVIDER_NAMES[key], key
            )
        self._liveapi_provider_combo.currentIndexChanged.connect(
            self._on_liveapi_provider_changed
        )
        prov_row.addWidget(self._liveapi_prov_label)
        prov_row.addWidget(self._liveapi_provider_combo, 1)
        body.addLayout(prov_row)

        # Model selector (repopulated per provider).
        model_row = QHBoxLayout()
        self._liveapi_model_label = QLabel(i18n.t("settings.model"))
        self._liveapi_model_label.setObjectName("fieldLabel")
        self._liveapi_model_label.setMinimumWidth(96)
        self._liveapi_model_label.setWordWrap(True)
        self._liveapi_model_combo = QComboBox()
        self._liveapi_model_combo.setObjectName("voiceCombo")
        self._liveapi_model_combo.currentIndexChanged.connect(
            self._on_liveapi_model_changed
        )
        model_row.addWidget(self._liveapi_model_label)
        model_row.addWidget(self._liveapi_model_combo, 1)
        body.addLayout(model_row)

        # "Naviguer" → official live/realtime docs page for the active provider.
        docs_row = QHBoxLayout()
        docs_row.addStretch()
        self._liveapi_docs_btn = QPushButton(i18n.t("provider.browse"))
        self._liveapi_docs_btn.setObjectName("ghostBtn")
        self._liveapi_docs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._liveapi_docs_btn.clicked.connect(self._on_liveapi_docs)
        docs_row.addWidget(self._liveapi_docs_btn)
        body.addLayout(docs_row)

        # API-key cards — one per provider, reusing the AI provider key store
        # (no duplicate storage). Only the active provider's card is shown.
        for key in self._LIVEAPI_PROVIDER_ORDER:
            card = _ProviderCard(self._LIVEAPI_KEY_PROVIDER[key])
            card.saved.connect(self._on_provider_saved)
            body.addWidget(card)
            self._liveapi_cards[key] = card

        # Violet info notice — STT (Whisper) + TTS are bypassed while active.
        self._liveapi_notice = QLabel(i18n.t("liveapi.notice"))
        self._liveapi_notice.setObjectName("liveApiNotice")
        self._liveapi_notice.setWordWrap(True)
        body.addWidget(self._liveapi_notice)

        l.addWidget(self._liveapi_body)
        return wrap

    def _populate_liveapi_provider(self, provider: str, model: str | None) -> None:
        """Fill the model combo for *provider* and reveal only its key card."""
        combo = self._liveapi_model_combo
        combo.blockSignals(True)
        combo.clear()
        for label, value in self._LIVEAPI_MODELS.get(provider, ()):
            combo.addItem(label, value)
        # Preserve a stored model that isn't in the predefined list so it isn't
        # silently dropped when the panel is reopened.
        if model and not any(combo.itemData(i) == model for i in range(combo.count())):
            combo.insertItem(0, model, model)
        if model:
            for i in range(combo.count()):
                if combo.itemData(i) == model:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        for live_provider, card in self._liveapi_cards.items():
            card.setVisible(live_provider == provider)

    def _apply_liveapi_visibility(self) -> None:
        self._liveapi_body.setVisible(self._liveapi_enabled_toggle.isChecked())

    def _on_liveapi_enabled_changed(self, _state) -> None:
        set_setting("liveapi_enabled", self._liveapi_enabled_toggle.isChecked())
        self._apply_liveapi_visibility()

    def _on_liveapi_provider_changed(self, _idx: int) -> None:
        provider = self._liveapi_provider_combo.currentData()
        if not provider:
            return
        set_setting("liveapi_provider", provider)
        # Switching provider invalidates the old model id — fall back to the
        # new provider's first model and persist it.
        models = self._LIVEAPI_MODELS.get(provider, ())
        default_model = models[0][1] if models else None
        set_setting("liveapi_model", default_model)
        self._populate_liveapi_provider(provider, default_model)

    def _on_liveapi_model_changed(self, _idx: int) -> None:
        model = self._liveapi_model_combo.currentData()
        if model:
            set_setting("liveapi_model", model)

    def _on_liveapi_docs(self) -> None:
        provider = self._liveapi_provider_combo.currentData()
        url = self._LIVEAPI_DOCS_URLS.get(provider)
        if url:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    # ── AI Task Hierarchy (Bloc 3) ──────────────────────────────────────────
    # Two ordered "provider:model" fallback chains (heavy / medium). The value
    # lists in ``self._hier_values`` are the source of truth; the editable row
    # widgets are a rendering of them. Any add/remove/reorder/selection rebuilds
    # the lists and writes them through to ``ai_hierarchy`` in settings.json.

    def _build_ai_hierarchy_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._hier_title = QLabel(i18n.t("settings.ai_hierarchy"))
        self._hier_title.setObjectName("sectionTitle")
        self._hier_title.setWordWrap(True)
        self._hier_sub = QLabel(i18n.t("settings.ai_hierarchy_sub"))
        self._hier_sub.setObjectName("sectionSub")
        self._hier_sub.setWordWrap(True)
        l.addWidget(self._hier_title)
        l.addWidget(self._hier_sub)

        # Per-category editor state.
        self._hier_values = {"heavy": [], "medium": []}
        self._hier_hosts = {}        # cat -> QVBoxLayout holding the rows
        self._hier_rows = {"heavy": [], "medium": []}   # cat -> [QComboBox]
        self._hier_labels = {}       # cat -> header QLabel (for retranslate)
        self._hier_sublabels = {}    # cat -> hint QLabel
        self._hier_add_btns = {}     # cat -> "add" QPushButton

        self._build_hier_subsection(
            l, "heavy", "settings.hierarchy_heavy", "settings.hierarchy_heavy_sub")
        self._build_hier_subsection(
            l, "medium", "settings.hierarchy_medium", "settings.hierarchy_medium_sub")

        # Master switch (bottom of the section): when OFF, OSIA ignores the
        # chains above and always uses the manually selected model. The chain
        # editors stay visible either way so they can be configured regardless.
        enable_row = QHBoxLayout()
        self._hier_enabled_label = QLabel(i18n.t("hierarchy.enabled"))
        self._hier_enabled_label.setObjectName("providerName")
        self._hier_enabled_label.setWordWrap(True)
        self._hier_enabled_toggle = QCheckBox()
        self._hier_enabled_toggle.setObjectName("smartToggle")
        self._hier_enabled_toggle.stateChanged.connect(self._on_hier_enabled_changed)
        enable_row.addWidget(self._hier_enabled_label)
        enable_row.addStretch()
        enable_row.addWidget(self._hier_enabled_toggle)
        l.addLayout(enable_row)

        return wrap

    def _build_hier_subsection(self, parent_layout, cat, label_key, sub_key) -> None:
        header = QLabel(i18n.t(label_key))
        header.setObjectName("providerName")
        header.setWordWrap(True)
        parent_layout.addWidget(header)

        sub = QLabel(i18n.t(sub_key))
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)
        parent_layout.addWidget(sub)

        host_wrap = QWidget()
        host = QVBoxLayout(host_wrap)
        host.setContentsMargins(0, 0, 0, 0)
        host.setSpacing(6)
        self._hier_hosts[cat] = host
        parent_layout.addWidget(host_wrap)

        add_btn = QPushButton("＋ " + i18n.t("settings.hierarchy_add"))
        add_btn.setObjectName("primaryBtn")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(lambda _=False, c=cat: self._on_hier_add(c))
        parent_layout.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignLeft)

        self._hier_labels[cat] = header
        self._hier_sublabels[cat] = sub
        self._hier_add_btns[cat] = add_btn

    def _hier_model_choices(self) -> list:
        """Return ``[(display, "provider:model"), …]`` for configured providers
        only (those with a saved API key, plus keyless Ollama)."""
        keys = load_api_keys()
        choices = []
        for m in get_all_models():
            provider = m["provider"]
            if provider != "ollama" and not keys.get(provider):
                continue
            pname = PROVIDERS.get(provider, {}).get("name", provider)
            choices.append((f"{pname} · {m.get('label', m['id'])}",
                            f"{provider}:{m['id']}"))
        choices.sort(key=lambda c: c[0].lower())
        return choices

    def _render_hier_category(self, cat: str) -> None:
        host = self._hier_hosts.get(cat)
        if host is None:
            return
        # Wipe & rebuild the rows for this category from the value list.
        while host.count():
            item = host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._hier_rows[cat] = []

        values = self._hier_values.get(cat, [])
        if not values:
            empty = QLabel(i18n.t("settings.hierarchy_empty"))
            empty.setObjectName("providerSub")
            empty.setWordWrap(True)
            host.addWidget(empty)
            return
        for idx, value in enumerate(values):
            host.addWidget(self._build_hier_row(cat, idx, value))

    def _build_hier_row(self, cat: str, index: int, value: str) -> QWidget:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        combo = QComboBox()
        combo.setObjectName("voiceCombo")
        choices = self._hier_model_choices()
        # Preserve a stored model whose provider is no longer configured by
        # showing its raw "provider:model" value so the chain isn't silently
        # dropped when the user opens settings.
        if value and not any(v == value for _, v in choices):
            combo.addItem(value, value)
        for label, v in choices:
            combo.addItem(label, v)
        combo.blockSignals(True)
        if value:
            for i in range(combo.count()):
                if combo.itemData(i) == value:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        combo.currentIndexChanged.connect(
            lambda _i, c=cat: self._on_hier_row_changed(c)
        )
        h.addWidget(combo, 1)
        self._hier_rows[cat].append(combo)

        up = QPushButton("▲")
        up.setObjectName("ghostBtn")
        up.setFixedSize(28, 28)
        up.setCursor(Qt.CursorShape.PointingHandCursor)
        up.clicked.connect(lambda _=False, c=cat, i=index: self._on_hier_move(c, i, -1))
        down = QPushButton("▼")
        down.setObjectName("ghostBtn")
        down.setFixedSize(28, 28)
        down.setCursor(Qt.CursorShape.PointingHandCursor)
        down.clicked.connect(lambda _=False, c=cat, i=index: self._on_hier_move(c, i, 1))
        rm = QPushButton("✕")
        rm.setObjectName("convDel")
        rm.setFixedSize(28, 28)
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.clicked.connect(lambda _=False, c=cat, i=index: self._on_hier_remove(c, i))
        h.addWidget(up)
        h.addWidget(down)
        h.addWidget(rm)
        return row

    def _persist_hierarchy(self) -> None:
        clean = {
            "heavy": [v for v in self._hier_values.get("heavy", []) if v],
            "medium": [v for v in self._hier_values.get("medium", []) if v],
        }
        set_setting("ai_hierarchy", clean)

    def _on_hier_add(self, cat: str) -> None:
        choices = self._hier_model_choices()
        if not choices:
            # No configured provider to pick from — don't seed a dead empty row.
            return
        self._hier_values.setdefault(cat, []).append(choices[0][1])
        self._persist_hierarchy()
        self._render_hier_category(cat)

    def _on_hier_remove(self, cat: str, index: int) -> None:
        values = self._hier_values.get(cat, [])
        if 0 <= index < len(values):
            values.pop(index)
            self._persist_hierarchy()
            self._render_hier_category(cat)

    def _on_hier_move(self, cat: str, index: int, delta: int) -> None:
        values = self._hier_values.get(cat, [])
        target = index + delta
        if 0 <= index < len(values) and 0 <= target < len(values):
            values[index], values[target] = values[target], values[index]
            self._persist_hierarchy()
            self._render_hier_category(cat)

    def _on_hier_row_changed(self, cat: str) -> None:
        # Re-read the chain straight from the current row order so a selection
        # change stays consistent with any pending reorder.
        self._hier_values[cat] = [
            c.currentData() for c in self._hier_rows.get(cat, []) if c.currentData()
        ]
        self._persist_hierarchy()

    def _on_hier_enabled_changed(self, _state) -> None:
        set_setting("ai_hierarchy_enabled", self._hier_enabled_toggle.isChecked())

    # ── Scheduled Tasks / Cron (Bloc 5) ─────────────────────────────────────
    # A dynamic list of cron jobs mirroring the AI-hierarchy editor: the list
    # ``self._cron_jobs`` is the source of truth and every add / remove / edit
    # rewrites ``cron_jobs`` in settings.json. Each job is
    # ``{"task": str, "schedule": str, "enabled": bool}`` where ``schedule`` is
    # the canonical string parsed by osia.cron ("every N minutes|hours|days" or
    # "at HH:MM").

    _CRON_MODES = ("minutes", "hours", "days", "daily")
    _CRON_MODE_KEYS = {
        "minutes": "cron.every_minutes",
        "hours": "cron.every_hours",
        "days": "cron.every_days",
        "daily": "cron.daily_at",
    }

    def _build_cron_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._cron_title = QLabel(i18n.t("settings.cron"))
        self._cron_title.setObjectName("sectionTitle")
        self._cron_title.setWordWrap(True)
        self._cron_sub = QLabel(i18n.t("settings.cron_sub"))
        self._cron_sub.setObjectName("sectionSub")
        self._cron_sub.setWordWrap(True)
        l.addWidget(self._cron_title)
        l.addWidget(self._cron_sub)

        # Source-of-truth job list + the layout its row widgets render into.
        self._cron_jobs: list[dict] = []
        host_wrap = QWidget()
        self._cron_host = QVBoxLayout(host_wrap)
        self._cron_host.setContentsMargins(0, 0, 0, 0)
        self._cron_host.setSpacing(8)
        l.addWidget(host_wrap)

        self._cron_add_btn = QPushButton("＋ " + i18n.t("cron.add"))
        self._cron_add_btn.setObjectName("primaryBtn")
        self._cron_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cron_add_btn.clicked.connect(self._on_cron_add)
        l.addWidget(self._cron_add_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return wrap

    def _render_cron(self) -> None:
        # Snapshot conversation titles once per render so each row's pill can
        # resolve its linked conversation id without re-reading the disk store.
        try:
            self._cron_conv_titles = {
                c.get("id"): c.get("title")
                for c in conversations.list_conversations()
            }
        except Exception:
            self._cron_conv_titles = {}
        host = self._cron_host
        while host.count():
            item = host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if not self._cron_jobs:
            empty = QLabel(i18n.t("cron.empty"))
            empty.setObjectName("providerSub")
            empty.setWordWrap(True)
            host.addWidget(empty)
            return
        for idx, job in enumerate(self._cron_jobs):
            host.addWidget(self._build_cron_row(idx, job))

    def _build_cron_row(self, index: int, job: dict) -> QWidget:
        row = QFrame()
        row.setObjectName("providerCard")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        # Row A — task description, conversation pill + picker, enable, remove.
        top = QHBoxLayout()
        top.setSpacing(6)
        task_input = QLineEdit(job.get("task", ""))
        task_input.setObjectName("keyInput")
        task_input.setPlaceholderText(i18n.t("cron.task_placeholder"))
        task_input.textChanged.connect(
            lambda txt, i=index: self._on_cron_field(i, "task", txt, persist=False)
        )
        task_input.editingFinished.connect(self._persist_cron)
        # Linked-conversation pill + chat-bubble button opening the picker.
        pill = QLabel(self._cron_pill_text(job.get("conversation_id")))
        pill.setObjectName("convPill")
        pill.setWordWrap(True)
        bubble = QPushButton("💬")
        bubble.setObjectName("ghostBtn")
        bubble.setFixedSize(28, 28)
        bubble.setCursor(Qt.CursorShape.PointingHandCursor)
        bubble.setToolTip(i18n.t("cron.conversation"))
        bubble.clicked.connect(
            lambda _=False, i=index, b=bubble: self._on_cron_pick_conversation(i, b)
        )
        enable = QCheckBox()
        enable.setObjectName("smartToggle")
        enable.setChecked(bool(job.get("enabled", False)))
        enable.stateChanged.connect(
            lambda _s, i=index, c=enable: self._on_cron_field(i, "enabled", c.isChecked())
        )
        rm = QPushButton("✕")
        rm.setObjectName("convDel")
        rm.setFixedSize(28, 28)
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.clicked.connect(lambda _=False, i=index: self._on_cron_remove(i))
        top.addWidget(task_input, 1)
        top.addWidget(pill)
        top.addWidget(bubble)
        top.addWidget(enable)
        top.addWidget(rm)
        v.addLayout(top)

        # Row B — schedule type (Repeat / Once) + its inputs. "Repeat" keeps the
        # recurring mode + value editor; "Once" swaps in a date + time picker.
        mode, value = self._parse_cron_schedule(job.get("schedule", ""))
        is_once = (mode == "once")
        bottom = QHBoxLayout()
        bottom.setSpacing(6)

        type_combo = QComboBox()
        type_combo.setObjectName("voiceCombo")
        type_combo.addItem(i18n.t("cron.type_repeat"), "repeat")
        type_combo.addItem(i18n.t("cron.type_once"), "once")
        type_combo.setCurrentIndex(1 if is_once else 0)
        type_combo.currentIndexChanged.connect(
            lambda _i, idx=index, tc=type_combo: self._on_cron_type_changed(idx, tc)
        )
        bottom.addWidget(type_combo)

        if is_once:
            date_str, time_str = self._once_fields_from_iso(value)
            date_input = QLineEdit(date_str)
            date_input.setObjectName("keyInput")
            date_input.setFixedWidth(110)
            date_input.setPlaceholderText(i18n.t("cron.once_date_placeholder"))
            time_input = QLineEdit(time_str)
            time_input.setObjectName("keyInput")
            time_input.setFixedWidth(90)
            time_input.setPlaceholderText(
                "2:30 PM" if timefmt.use_12h() else "14:30")
            for fld in (date_input, time_input):
                fld.textChanged.connect(
                    lambda _t, idx=index, di=date_input, ti=time_input:
                        self._on_cron_once_changed(idx, di, ti, persist=False)
                )
                fld.editingFinished.connect(self._persist_cron)
            bottom.addWidget(date_input)
            bottom.addWidget(time_input)
            bottom.addStretch()
        else:
            mode_combo = QComboBox()
            mode_combo.setObjectName("voiceCombo")
            for m in self._CRON_MODES:
                mode_combo.addItem(i18n.t(self._CRON_MODE_KEYS[m]), m)
            for i in range(mode_combo.count()):
                if mode_combo.itemData(i) == mode:
                    mode_combo.setCurrentIndex(i)
                    break
            value_input = QLineEdit(value)
            value_input.setObjectName("keyInput")
            value_input.setFixedWidth(90)
            value_input.setPlaceholderText(
                ("2:30 PM" if timefmt.use_12h() else "14:30") if mode == "daily" else "5")
            mode_combo.currentIndexChanged.connect(
                lambda _i, idx=index, mc=mode_combo, vi=value_input:
                    self._on_cron_schedule_changed(idx, mc, vi)
            )
            value_input.textChanged.connect(
                lambda _t, idx=index, mc=mode_combo, vi=value_input:
                    self._on_cron_schedule_changed(idx, mc, vi, persist=False)
            )
            value_input.editingFinished.connect(self._persist_cron)
            bottom.addWidget(mode_combo, 1)
            bottom.addWidget(value_input)
        v.addLayout(bottom)
        return row

    @staticmethod
    def _parse_cron_schedule(schedule: str) -> tuple[str, str]:
        """Split a canonical schedule string into ``(mode, value)`` for the UI.

        For one-shot jobs ``mode`` is ``"once"`` and ``value`` is the ISO
        ``YYYY-MM-DDTHH:MM`` string (split into date + time fields by the row
        builder). Recurring jobs return their interval/daily mode as before.
        """
        schedule = schedule or ""
        try:
            from osia.cron import parse_schedule
            spec = parse_schedule(schedule)
        except Exception:
            spec = None
        if spec is not None and spec["kind"] == "once":
            dt = spec["dt"]
            return ("once",
                    f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
                    f"T{dt.hour:02d}:{dt.minute:02d}")
        if spec is None:
            # Unparseable: keep a half-typed one-shot on the "Once" tab so the
            # user can finish entering it; otherwise default to the interval tab.
            if schedule.strip().lower().startswith("once:"):
                return ("once", "")
            return ("minutes", "")
        if spec["kind"] == "daily":
            return ("daily", timefmt.format_hm(spec["hour"], spec["minute"]))
        secs = spec["seconds"]
        if secs % 86400 == 0:
            return ("days", str(secs // 86400))
        if secs % 3600 == 0:
            return ("hours", str(secs // 3600))
        return ("minutes", str(secs // 60))

    @staticmethod
    def _once_fields_from_iso(iso: str) -> tuple[str, str]:
        """Split an ISO ``YYYY-MM-DDTHH:MM`` into ``(DD/MM/YYYY, time)`` for the
        date + time inputs. The time respects the 12h/24h setting. A partial /
        empty ISO yields empty fields."""
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$", iso or "")
        if not m:
            return ("", "")
        y, mo, d, h, mi = (int(g) for g in m.groups())
        return (f"{d:02d}/{mo:02d}/{y:04d}", timefmt.format_hm(h, mi))

    @staticmethod
    def _parse_time_display(text: str) -> tuple[int, int] | None:
        """Parse a ``HH:MM`` (24h) or ``H:MM AM/PM`` (12h) string into a 24-hour
        ``(hour, minute)`` pair, or ``None`` when it isn't a valid time."""
        s = (text or "").strip().lower()
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", s)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.match(r"^(1[0-2]|0?[1-9]):([0-5]\d)\s*(am|pm)$", s)
        if m:
            hour = int(m.group(1)) % 12
            if m.group(3) == "pm":
                hour += 12
            return (hour, int(m.group(2)))
        return None

    @classmethod
    def _compose_once_schedule(cls, date_str: str, time_str: str) -> str:
        """Build a canonical ``once:YYYY-MM-DDTHH:MM`` string from the date
        (DD/MM/YYYY) and time fields. Incomplete/invalid input yields a bare
        ``"once:"`` that the scheduler ignores until it's corrected, while still
        keeping the row on the "Once" tab."""
        dm = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", date_str or "")
        hm = cls._parse_time_display(time_str)
        if not dm or hm is None:
            return "once:"
        day, month, year = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        hour, minute = hm
        try:
            datetime(year, month, day, hour, minute)  # reject impossible dates
        except ValueError:
            return "once:"
        return f"once:{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"

    @staticmethod
    def _compose_cron_schedule(mode: str, value: str) -> str:
        """Build the canonical schedule string from the UI ``(mode, value)``."""
        value = (value or "").strip()
        if mode == "daily":
            return f"at {value}"
        unit = mode if mode in ("minutes", "hours", "days") else "minutes"
        return f"every {value} {unit}"

    def _on_cron_field(self, index: int, field: str, value, persist: bool = True) -> None:
        if 0 <= index < len(self._cron_jobs):
            self._cron_jobs[index][field] = value
            if persist:
                self._persist_cron()

    def _on_cron_schedule_changed(self, index: int, mode_combo: QComboBox,
                                  value_input: QLineEdit, persist: bool = True) -> None:
        mode = mode_combo.currentData() or "minutes"
        value_input.setPlaceholderText(
            ("2:30 PM" if timefmt.use_12h() else "14:30") if mode == "daily" else "5")
        if 0 <= index < len(self._cron_jobs):
            self._cron_jobs[index]["schedule"] = self._compose_cron_schedule(
                mode, value_input.text()
            )
            if persist:
                self._persist_cron()

    def _on_cron_type_changed(self, index: int, type_combo: QComboBox) -> None:
        """Switch a job between a recurring schedule and a single run. Seeds a
        sensible default (top of the next hour for "Once", every 30 min for
        "Repeat") and re-renders so the matching inputs appear."""
        if not (0 <= index < len(self._cron_jobs)):
            return
        typ = type_combo.currentData() or "repeat"
        if typ == "once":
            dt = (datetime.now() + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0)
            self._cron_jobs[index]["schedule"] = (
                f"once:{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
                f"T{dt.hour:02d}:{dt.minute:02d}")
        else:
            self._cron_jobs[index]["schedule"] = "every 30 minutes"
        self._persist_cron()
        self._render_cron()

    def _on_cron_once_changed(self, index: int, date_input: QLineEdit,
                              time_input: QLineEdit, persist: bool = True) -> None:
        if 0 <= index < len(self._cron_jobs):
            self._cron_jobs[index]["schedule"] = self._compose_once_schedule(
                date_input.text(), time_input.text()
            )
            if persist:
                self._persist_cron()

    def _on_cron_add(self) -> None:
        self._cron_jobs.append(
            {"task": "", "schedule": "every 30 minutes", "enabled": False,
             "conversation_id": None}
        )
        self._persist_cron()
        self._render_cron()

    def _on_cron_remove(self, index: int) -> None:
        if 0 <= index < len(self._cron_jobs):
            self._cron_jobs.pop(index)
            self._persist_cron()
            self._render_cron()

    def _cron_pill_text(self, conv_id) -> str:
        """Short pill label for a cron row's linked conversation: the
        (truncated) title, or "None" when unset or the conversation is gone."""
        if not conv_id:
            return i18n.t("cron.conversation_none")
        title = (getattr(self, "_cron_conv_titles", {}) or {}).get(conv_id)
        if not title:
            return i18n.t("cron.conversation_none")
        if len(title) > 18:
            title = title[:17].rstrip() + "…"
        return title

    def _on_cron_pick_conversation(self, index: int, anchor=None) -> None:
        if not (0 <= index < len(self._cron_jobs)):
            return
        current = self._cron_jobs[index].get("conversation_id")
        dlg = _CronConversationDialog(self, current, anchor=anchor)
        # exec() returns truthy only when a row was chosen (accept); Esc/cancel
        # leaves the link unchanged.
        if dlg.exec():
            self._cron_jobs[index]["conversation_id"] = dlg.result_id
            self._persist_cron()
            self._render_cron()

    def _persist_cron(self) -> None:
        clean = []
        for job in self._cron_jobs:
            clean.append({
                "task": (job.get("task") or "").strip(),
                "schedule": (job.get("schedule") or "").strip(),
                "enabled": bool(job.get("enabled", False)),
                "conversation_id": job.get("conversation_id") or None,
            })
        set_setting("cron_jobs", clean)

    # ── Heartbeat / system monitoring (Bloc 5) ──────────────────────────────
    # Master toggle + collapsible body: an interval field and one row per
    # monitored metric (CPU / RAM / disk / network). Everything writes straight
    # through to ``heartbeat_*`` in settings.json so the background monitor
    # (osia.heartbeat) picks the changes up on its next cycle.

    _HB_METRICS = (
        # (name, label_key, hint_key, has_threshold)
        ("cpu", "heartbeat.check_cpu", "heartbeat.hint_usage", True),
        ("ram", "heartbeat.check_ram", "heartbeat.hint_usage", True),
        ("disk", "heartbeat.check_disk", "heartbeat.hint_disk", True),
        ("network", "heartbeat.check_network", "heartbeat.hint_network", False),
    )
    _HB_DEFAULT_THRESHOLDS = {"cpu": 80, "ram": 90, "disk": 10}

    def _build_heartbeat_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._hb_title = QLabel(i18n.t("settings.heartbeat"))
        self._hb_title.setObjectName("sectionTitle")
        self._hb_title.setWordWrap(True)
        self._hb_sub = QLabel(i18n.t("settings.heartbeat_sub"))
        self._hb_sub.setObjectName("sectionSub")
        self._hb_sub.setWordWrap(True)
        l.addWidget(self._hb_title)
        l.addWidget(self._hb_sub)

        # Master toggle — the body below stays hidden until ON.
        enable_row = QHBoxLayout()
        self._hb_enabled_label = QLabel(i18n.t("heartbeat.enabled"))
        self._hb_enabled_label.setObjectName("providerName")
        self._hb_enabled_label.setWordWrap(True)
        self._hb_enabled_toggle = QCheckBox()
        self._hb_enabled_toggle.setObjectName("smartToggle")
        self._hb_enabled_toggle.stateChanged.connect(self._on_hb_enabled_changed)
        enable_row.addWidget(self._hb_enabled_label)
        enable_row.addStretch()
        enable_row.addWidget(self._hb_enabled_toggle)
        l.addLayout(enable_row)

        self._hb_body = QWidget()
        body = QVBoxLayout(self._hb_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        # Sampling interval (minutes).
        interval_row = QHBoxLayout()
        self._hb_interval_label = QLabel(i18n.t("heartbeat.interval"))
        self._hb_interval_label.setObjectName("fieldLabel")
        self._hb_interval_label.setWordWrap(True)
        self._hb_interval_input = QLineEdit()
        self._hb_interval_input.setObjectName("keyInput")
        self._hb_interval_input.setFixedWidth(70)
        self._hb_interval_input.editingFinished.connect(self._on_hb_interval_changed)
        interval_row.addWidget(self._hb_interval_label)
        interval_row.addStretch()
        interval_row.addWidget(self._hb_interval_input)
        body.addLayout(interval_row)

        # Per-metric rows.
        self._hb_check_toggles: dict[str, QCheckBox] = {}
        self._hb_threshold_inputs: dict[str, QLineEdit] = {}
        self._hb_metric_labels: dict[str, QLabel] = {}
        self._hb_metric_hints: dict[str, QLabel] = {}
        for name, label_key, hint_key, has_threshold in self._HB_METRICS:
            body.addWidget(
                self._build_hb_metric_row(name, label_key, hint_key, has_threshold)
            )

        l.addWidget(self._hb_body)
        return wrap

    def _build_hb_metric_row(self, name: str, label_key: str, hint_key: str,
                             has_threshold: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("providerCard")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        toggle = QCheckBox()
        toggle.setObjectName("smartToggle")
        toggle.stateChanged.connect(self._persist_heartbeat_checks)
        label = QLabel(i18n.t(label_key))
        label.setObjectName("providerName")
        label.setWordWrap(True)
        top.addWidget(toggle)
        top.addWidget(label)
        top.addStretch()
        if has_threshold:
            thr = QLineEdit()
            thr.setObjectName("keyInput")
            thr.setFixedWidth(56)
            thr.editingFinished.connect(self._persist_heartbeat_checks)
            pct = QLabel("%")
            pct.setObjectName("providerSub")
            pct.setWordWrap(True)
            top.addWidget(thr)
            top.addWidget(pct)
            self._hb_threshold_inputs[name] = thr
        v.addLayout(top)

        hint = QLabel(i18n.t(hint_key))
        hint.setObjectName("sectionSub")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self._hb_check_toggles[name] = toggle
        self._hb_metric_labels[name] = label
        self._hb_metric_hints[name] = hint
        return row

    def _apply_heartbeat_visibility(self) -> None:
        self._hb_body.setVisible(self._hb_enabled_toggle.isChecked())

    def _on_hb_enabled_changed(self, _state) -> None:
        set_setting("heartbeat_enabled", self._hb_enabled_toggle.isChecked())
        self._apply_heartbeat_visibility()

    def _on_hb_interval_changed(self) -> None:
        try:
            minutes = max(1, int(float(self._hb_interval_input.text().strip())))
        except (TypeError, ValueError):
            minutes = 5
        self._hb_interval_input.blockSignals(True)
        self._hb_interval_input.setText(str(minutes))
        self._hb_interval_input.blockSignals(False)
        set_setting("heartbeat_interval_minutes", minutes)

    def _persist_heartbeat_checks(self) -> None:
        checks = {}
        for name, toggle in self._hb_check_toggles.items():
            entry = {"enabled": bool(toggle.isChecked())}
            thr = self._hb_threshold_inputs.get(name)
            if thr is not None:
                try:
                    entry["threshold"] = max(0, min(100, int(float(thr.text().strip()))))
                except (TypeError, ValueError):
                    entry["threshold"] = self._HB_DEFAULT_THRESHOLDS.get(name, 0)
            checks[name] = entry
        set_setting("heartbeat_checks", checks)

    # ── API key fallback hierarchy ──────────────────────────────────────────
    # A dropdown of configured providers; picking one opens a dark popover
    # (_ApiKeyFallbackDialog) with an ordered list of masked key inputs —
    # key1 = primary, the rest tried in order on a 429/auth error before the AI
    # Task Hierarchy switches models. Persisted as ``api_key_fallbacks`` in
    # settings.json. See OSIAAgent.set_model/_activate_next_api_key/run().

    def _build_apikeys_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._apikeys_title = QLabel(i18n.t("settings.api_keys"))
        self._apikeys_title.setObjectName("sectionTitle")
        self._apikeys_title.setWordWrap(True)
        self._apikeys_sub = QLabel(i18n.t("settings.api_keys_sub"))
        self._apikeys_sub.setObjectName("sectionSub")
        self._apikeys_sub.setWordWrap(True)
        l.addWidget(self._apikeys_title)
        l.addWidget(self._apikeys_sub)

        self._apikeys_combo = QComboBox()
        self._apikeys_combo.setObjectName("voiceCombo")
        # ``activated`` fires on user selection (even re-picking the same item),
        # unlike ``currentIndexChanged`` — so the popover reopens every time.
        self._apikeys_combo.activated.connect(self._on_apikeys_provider_activated)
        l.addWidget(self._apikeys_combo)

        self._apikeys_hint = QLabel(i18n.t("api_keys.no_providers"))
        self._apikeys_hint.setObjectName("providerSub")
        self._apikeys_hint.setWordWrap(True)
        l.addWidget(self._apikeys_hint)

        self._refresh_apikeys_providers()
        return wrap

    def _apikeys_provider_chain(self, provider: str) -> list:
        """The saved api_key_fallbacks list for a provider (blanks dropped)."""
        fallbacks = load_settings().get("api_key_fallbacks", {}) or {}
        return [k for k in (fallbacks.get(provider) or [])
                if isinstance(k, str) and k.strip()]

    def _refresh_apikeys_providers(self) -> None:
        """Rebuild the provider dropdown from the currently configured providers
        (those with a saved key or an existing fallback list). The leading
        placeholder keeps the combo in a neutral state; each provider item shows
        its configured key count."""
        combo = getattr(self, "_apikeys_combo", None)
        if combo is None:
            return
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(i18n.t("api_keys.select_provider"), None)
        keys = load_api_keys()
        count = 0
        for provider in self._AI_PROVIDER_ORDER:
            chain = self._apikeys_provider_chain(provider)
            if not keys.get(provider) and not chain:
                continue
            name = PROVIDERS.get(provider, {}).get("name", provider)
            label = f"{name}  ·  {len(chain)}" if chain else name
            combo.addItem(label, provider)
            count += 1
        combo.setCurrentIndex(0)
        combo.blockSignals(False)
        if hasattr(self, "_apikeys_hint"):
            self._apikeys_hint.setVisible(count == 0)

    def _on_apikeys_provider_activated(self, _index: int) -> None:
        provider = self._apikeys_combo.currentData()
        if not provider:
            return
        name = PROVIDERS.get(provider, {}).get("name", provider)
        keys = self._apikeys_provider_chain(provider)
        if not keys:
            # Convenience: seed key1 with the provider's existing saved key so
            # the primary starts in sync with the AI Providers section.
            primary = get_api_key(provider)
            keys = [primary] if primary else [""]
        dlg = _ApiKeyFallbackDialog(self, provider, name, keys,
                                    anchor=self._apikeys_combo)
        dlg.exec()
        # The dialog persisted on close; refresh the per-provider key counts and
        # reset the combo to its neutral placeholder.
        self._refresh_apikeys_providers()

    # ── Mobile connection (pairing token) ──────────────────────────────────

    def _build_mobile_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._mobile_title = QLabel(i18n.t("settings.mobile"))
        self._mobile_title.setObjectName("sectionTitle")
        self._mobile_title.setWordWrap(True)
        self._mobile_sub = QLabel(i18n.t("settings.mobile_sub"))
        self._mobile_sub.setObjectName("sectionSub")
        self._mobile_sub.setWordWrap(True)
        l.addWidget(self._mobile_title)
        l.addWidget(self._mobile_sub)

        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        self._mobile_token_label = QLabel(i18n.t("settings.mobile_token"))
        self._mobile_token_label.setObjectName("fieldLabel")
        self._mobile_token_label.setMinimumWidth(60)
        self._mobile_token_label.setWordWrap(True)
        self._token_mask_label = QLabel(_mask_token(""))
        self._token_mask_label.setObjectName("tokenMask")
        self._token_mask_label.setWordWrap(True)
        self._token_mask_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction
        )
        self._mobile_show_btn = QPushButton(i18n.t("settings.show"))
        self._mobile_show_btn.setObjectName("ghostBtn")
        self._mobile_show_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mobile_show_btn.clicked.connect(self._on_show_token)
        self._mobile_regen_btn = QPushButton(i18n.t("settings.regenerate"))
        self._mobile_regen_btn.setObjectName("ghostBtn")
        self._mobile_regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mobile_regen_btn.clicked.connect(self._on_regenerate_token)
        token_row.addWidget(self._mobile_token_label)
        token_row.addWidget(self._token_mask_label, 1)
        token_row.addWidget(self._mobile_show_btn)
        token_row.addWidget(self._mobile_regen_btn)
        l.addLayout(token_row)

        self._mobile_warn = QLabel(i18n.t("settings.mobile_token_warning"))
        self._mobile_warn.setObjectName("warnLabel")
        self._mobile_warn.setWordWrap(True)
        l.addWidget(self._mobile_warn)

        return wrap

    # ── Section 1: AI providers (text / chat models) ───────────────────────

    def _build_ai_providers_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._ai_title = QLabel(i18n.t("settings.ai_providers"))
        self._ai_title.setObjectName("sectionTitle")
        self._ai_title.setWordWrap(True)
        self._ai_sub = QLabel(i18n.t("settings.ai_providers_sub"))
        self._ai_sub.setObjectName("sectionSub")
        self._ai_sub.setWordWrap(True)
        l.addWidget(self._ai_title)
        l.addWidget(self._ai_sub)

        # Compact "Choose provider" dropdown: picking a provider reveals only
        # that provider's configuration card below, instead of stacking every
        # provider's full card at once.
        sel_row = QHBoxLayout()
        self._ai_prov_label = QLabel(i18n.t("settings.choose_provider"))
        self._ai_prov_label.setObjectName("fieldLabel")
        self._ai_prov_label.setMinimumWidth(96)
        self._ai_prov_label.setWordWrap(True)
        self._ai_provider_combo = QComboBox()
        self._ai_provider_combo.setObjectName("voiceCombo")
        for provider in (*self._AI_PROVIDER_ORDER, "ollama"):
            self._ai_provider_combo.addItem(PROVIDERS[provider]["name"], provider)
        sel_row.addWidget(self._ai_prov_label)
        sel_row.addWidget(self._ai_provider_combo, 1)
        l.addLayout(sel_row)

        # One configuration card per API provider. Each card carries its own
        # "Configurer une clé API" + Naviguer row, key input and Save button
        # (with the green flash) — see _ProviderCard. Only the selected
        # provider's card is shown; the rest stay hidden.
        for provider in self._AI_PROVIDER_ORDER:
            card = _ProviderCard(provider)
            card.saved.connect(self._on_provider_saved)
            card.setVisible(False)
            l.addWidget(card)
            self._provider_cards[provider] = card

        # Ollama is keyless — its "card" is an info-only row, revealed the same
        # way when Ollama is the chosen provider.
        self._ollama_row = QFrame()
        self._ollama_row.setObjectName("providerCard")
        self._ollama_row.setVisible(False)
        ol = QVBoxLayout(self._ollama_row)
        ol.setContentsMargins(12, 10, 12, 10)
        olh = QHBoxLayout()
        olh.setSpacing(8)
        ol_name = QLabel(PROVIDERS["ollama"]["name"])
        ol_name.setObjectName("providerName")
        ol_name.setWordWrap(True)
        olh.addWidget(ol_name)
        olh.addStretch()
        self._ollama_tag = QLabel(i18n.t("provider.no_key_required"))
        self._ollama_tag.setObjectName("providerSub")
        self._ollama_tag.setWordWrap(True)
        i18n.add_listener(
            lambda: self._ollama_tag.setText(i18n.t("provider.no_key_required"))
        )
        olh.addWidget(self._ollama_tag)
        ol.addLayout(olh)
        l.addWidget(self._ollama_row)

        # Wire the selector and reveal the first provider's card.
        self._ai_provider_combo.currentIndexChanged.connect(
            self._on_ai_provider_changed
        )
        self._on_ai_provider_changed(self._ai_provider_combo.currentIndex())

        return wrap

    def _on_ai_provider_changed(self, _idx: int) -> None:
        provider = self._ai_provider_combo.currentData()
        for p, card in self._provider_cards.items():
            card.setVisible(p == provider)
        self._ollama_row.setVisible(provider == "ollama")

    # ── State load / save ─────────────────────────────────────────────────

    def _load_state(self) -> None:
        self._sync_all_cards_keys()

        settings = load_settings()

        lang = settings.get("language", i18n.DEFAULT_LANG) or i18n.DEFAULT_LANG
        self._language_combo.blockSignals(True)
        for i in range(self._language_combo.count()):
            if self._language_combo.itemData(i) == lang:
                self._language_combo.setCurrentIndex(i)
                break
        self._language_combo.blockSignals(False)

        # Time format (12h / 24h)
        tf = settings.get("time_format", "24h") or "24h"
        self._timefmt_combo.blockSignals(True)
        for i in range(self._timefmt_combo.count()):
            if self._timefmt_combo.itemData(i) == tf:
                self._timefmt_combo.setCurrentIndex(i)
                break
        self._timefmt_combo.blockSignals(False)

        # Floating capsule (WOR-22) — visibility from settings, autostart from
        # the actual autostart entry so the toggle mirrors reality.
        self._show_capsule_toggle.blockSignals(True)
        self._show_capsule_toggle.setChecked(
            bool(settings.get("show_capsule", True)))
        self._show_capsule_toggle.blockSignals(False)
        self._autostart_toggle.blockSignals(True)
        self._autostart_toggle.setChecked(autostart_enabled())
        self._autostart_toggle.blockSignals(False)

        # Voice (TTS)
        provider = settings.get("tts_provider", "local") or "local"
        voice = settings.get("tts_voice")
        self._tts_provider_combo.blockSignals(True)
        for i in range(self._tts_provider_combo.count()):
            if self._tts_provider_combo.itemData(i) == provider:
                self._tts_provider_combo.setCurrentIndex(i)
                break
        self._tts_provider_combo.blockSignals(False)
        self._populate_voice_combo(provider, voice)

        # Wake-word greeting — empty stored text shows the active language's
        # default as a hint; it's only persisted once the toggle is enabled or
        # the user edits the field.
        self._wake_enabled_toggle.blockSignals(True)
        self._wake_enabled_toggle.setChecked(
            bool(settings.get("wake_greeting_enabled", False)))
        self._wake_enabled_toggle.blockSignals(False)
        wake_text = (settings.get("wake_greeting_text") or "").strip()
        self._wake_text_input.blockSignals(True)
        self._wake_text_input.setText(wake_text or i18n.t("wake.greeting_default"))
        self._wake_text_input.blockSignals(False)

        # Whisper (STT)
        stt_provider = settings.get("stt_provider", "local") or "local"
        self._stt_provider_combo.blockSignals(True)
        for i in range(self._stt_provider_combo.count()):
            if self._stt_provider_combo.itemData(i) == stt_provider:
                self._stt_provider_combo.setCurrentIndex(i)
                break
        self._stt_provider_combo.blockSignals(False)
        self._populate_stt_model_combo(stt_provider, self._stt_selected_model(settings, stt_provider))

        # Whisper fallback provider + its model sub-option
        stt_fallback = settings.get("stt_fallback_provider", "local") or "local"
        self._stt_fallback_combo.blockSignals(True)
        for i in range(self._stt_fallback_combo.count()):
            if self._stt_fallback_combo.itemData(i) == stt_fallback:
                self._stt_fallback_combo.setCurrentIndex(i)
                break
        self._stt_fallback_combo.blockSignals(False)
        self._populate_stt_fallback_model_combo(
            stt_fallback, self._stt_selected_model(settings, stt_fallback)
        )

        # AI task hierarchy (Bloc 3) — rebuild both fallback-chain editors.
        hierarchy = settings.get("ai_hierarchy", {}) or {}
        self._hier_values = {
            "heavy": [v for v in (hierarchy.get("heavy") or [])
                      if isinstance(v, str) and v],
            "medium": [v for v in (hierarchy.get("medium") or [])
                       if isinstance(v, str) and v],
        }
        self._render_hier_category("heavy")
        self._render_hier_category("medium")
        self._hier_enabled_toggle.blockSignals(True)
        self._hier_enabled_toggle.setChecked(
            bool(settings.get("ai_hierarchy_enabled", False)))
        self._hier_enabled_toggle.blockSignals(False)

        # Live API (Bloc 4) — provider selector, its model sub-option, master
        # toggle, and the collapsed/expanded state of the body.
        liveapi_provider = settings.get("liveapi_provider", "google") or "google"
        # Coerce a stale / hand-edited provider that's no longer offered back to
        # a known one so the combo, model list and visible key card stay in sync.
        if liveapi_provider not in self._LIVEAPI_PROVIDER_ORDER:
            liveapi_provider = "google"
        self._liveapi_provider_combo.blockSignals(True)
        for i in range(self._liveapi_provider_combo.count()):
            if self._liveapi_provider_combo.itemData(i) == liveapi_provider:
                self._liveapi_provider_combo.setCurrentIndex(i)
                break
        self._liveapi_provider_combo.blockSignals(False)
        self._populate_liveapi_provider(
            liveapi_provider, settings.get("liveapi_model")
        )
        self._liveapi_enabled_toggle.blockSignals(True)
        self._liveapi_enabled_toggle.setChecked(
            bool(settings.get("liveapi_enabled", False)))
        self._liveapi_enabled_toggle.blockSignals(False)
        self._apply_liveapi_visibility()

        # Scheduled tasks (Bloc 5) — rebuild the cron job editor.
        cron_jobs = settings.get("cron_jobs", []) or []
        self._cron_jobs = [
            {
                "task": str(j.get("task", "")),
                "schedule": str(j.get("schedule", "")),
                "enabled": bool(j.get("enabled", False)),
                "conversation_id": j.get("conversation_id") or None,
            }
            for j in cron_jobs if isinstance(j, dict)
        ]
        self._render_cron()

        # Heartbeat (Bloc 5) — master toggle, interval, per-metric checks.
        self._hb_enabled_toggle.blockSignals(True)
        self._hb_enabled_toggle.setChecked(
            bool(settings.get("heartbeat_enabled", False)))
        self._hb_enabled_toggle.blockSignals(False)
        interval = settings.get("heartbeat_interval_minutes", 5) or 5
        self._hb_interval_input.blockSignals(True)
        self._hb_interval_input.setText(str(interval))
        self._hb_interval_input.blockSignals(False)
        hb_checks = settings.get("heartbeat_checks", {}) or {}
        for name, toggle in self._hb_check_toggles.items():
            cfg = hb_checks.get(name) or {}
            toggle.blockSignals(True)
            toggle.setChecked(bool(cfg.get("enabled", False)))
            toggle.blockSignals(False)
            thr = self._hb_threshold_inputs.get(name)
            if thr is not None:
                thr.blockSignals(True)
                thr.setText(str(cfg.get(
                    "threshold", self._HB_DEFAULT_THRESHOLDS.get(name, 0))))
                thr.blockSignals(False)
        self._apply_heartbeat_visibility()

        # API key fallback hierarchy — rebuild the provider dropdown (configured
        # providers can change as keys are added/removed elsewhere). The keys
        # themselves live behind the per-provider popover, not in this panel.
        self._refresh_apikeys_providers()

        # Mobile token (masked)
        self._mobile_token = load_mobile_token()
        self._update_token_mask()

    @staticmethod
    def _stt_selected_model(settings: dict, provider: str) -> str | None:
        if provider == "groq":
            return settings.get("stt_groq_model")
        return settings.get("stt_local_model")

    def _sync_all_cards_keys(self) -> None:
        """Push the saved keys into every provider card across both the AI and
        Voice sections so duplicate (openai/google) cards stay in sync."""
        keys = load_api_keys()
        for provider, card in self._provider_cards.items():
            card.set_key(keys.get(provider, ""))
        for provider, card in self._tts_cards.items():
            card.set_key(keys.get(provider, ""))
        # Live API cards are keyed by Live-API provider but reuse the matching
        # AI provider's stored key.
        for live_provider, card in self._liveapi_cards.items():
            key_provider = self._LIVEAPI_KEY_PROVIDER.get(live_provider, live_provider)
            card.set_key(keys.get(key_provider, ""))

    def _populate_voice_combo(self, provider: str, selected: str | None) -> None:
        combo = self._tts_voice_combo
        combo.blockSignals(True)
        combo.clear()
        if provider == "openai":
            for v in self._openai_voices:
                combo.addItem(v, v)
        elif provider == "google":
            for label, v in self._google_voices:
                combo.addItem(label, v)
        if selected:
            for i in range(combo.count()):
                if combo.itemData(i) == selected:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        self._tts_voice_row.setVisible(provider != "local")
        # Reveal only the chosen provider's config: the Kokoro info row for the
        # local provider, or the matching cloud key card for openai/google.
        self._kokoro_row.setVisible(provider == "local")
        for p, card in self._tts_cards.items():
            card.setVisible(p == provider)

    def _populate_stt_model_combo(self, provider: str, selected: str | None) -> None:
        combo = self._stt_model_combo
        combo.blockSignals(True)
        combo.clear()
        is_groq = provider == "groq"
        if is_groq:
            # Groq Whisper runs server-side, so every size is always available.
            for label, value in self._STT_GROQ_MODELS:
                combo.addItem(label, value)
        else:
            # Local faster-whisper: only sizes downloaded to the HF hub cache can
            # actually be loaded. Offer the present ones; list the rest greyed
            # out and unselectable so they're discoverable but can't be chosen.
            downloaded = _downloaded_whisper_models()
            for label, value in self._STT_LOCAL_MODELS:
                if value in downloaded:
                    combo.addItem(label, value)
                else:
                    combo.addItem(
                        i18n.t("stt.model_not_downloaded", name=label), value
                    )
                    combo.model().item(combo.count() - 1).setEnabled(False)
            # Never land the selection on a size that isn't downloaded.
            if selected not in downloaded:
                selected = next(
                    (v for _, v in self._STT_LOCAL_MODELS if v in downloaded), None
                )
        if selected:
            for i in range(combo.count()):
                if combo.itemData(i) == selected and combo.model().item(i).isEnabled():
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        # The free-tier badge + Groq-key hint only apply to Groq Whisper.
        self._stt_groq_badge.setVisible(is_groq)
        self._stt_groq_hint.setVisible(is_groq)

    def _populate_stt_fallback_model_combo(self, fallback: str, selected: str | None) -> None:
        # The fallback's model sub-option mirrors the primary one: Groq model
        # ids when the fallback is Groq, downloaded faster-whisper sizes when
        # local. The whole row is hidden when the fallback is "None".
        self._stt_fallback_model_row.setVisible(fallback not in (None, "none"))
        if fallback in (None, "none"):
            return
        combo = self._stt_fallback_model_combo
        combo.blockSignals(True)
        combo.clear()
        if fallback == "groq":
            for label, value in self._STT_GROQ_MODELS:
                combo.addItem(label, value)
        else:
            downloaded = _downloaded_whisper_models()
            for label, value in self._STT_LOCAL_MODELS:
                if value in downloaded:
                    combo.addItem(label, value)
                else:
                    combo.addItem(
                        i18n.t("stt.model_not_downloaded", name=label), value
                    )
                    combo.model().item(combo.count() - 1).setEnabled(False)
            if selected not in downloaded:
                selected = next(
                    (v for _, v in self._STT_LOCAL_MODELS if v in downloaded), None
                )
        if selected:
            for i in range(combo.count()):
                if combo.itemData(i) == selected and combo.model().item(i).isEnabled():
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)

    # ── Handlers ──────────────────────────────────────────────────────────

    def _on_tts_provider_changed(self, _idx: int) -> None:
        provider = self._tts_provider_combo.currentData()
        set_setting("tts_provider", provider)
        default_voice = self._provider_default_voice.get(provider)
        set_setting("tts_voice", default_voice)
        self._populate_voice_combo(provider, default_voice)

    def _on_tts_voice_changed(self, _idx: int) -> None:
        voice = self._tts_voice_combo.currentData()
        if voice:
            set_setting("tts_voice", voice)

    def _on_stt_provider_changed(self, _idx: int) -> None:
        provider = self._stt_provider_combo.currentData()
        if not provider:
            return
        set_setting("stt_provider", provider)
        selected = self._stt_selected_model(load_settings(), provider)
        self._populate_stt_model_combo(provider, selected)

    def _on_stt_model_changed(self, _idx: int) -> None:
        model = self._stt_model_combo.currentData()
        if not model:
            return
        provider = self._stt_provider_combo.currentData()
        key = "stt_groq_model" if provider == "groq" else "stt_local_model"
        set_setting(key, model)

    def _on_stt_fallback_changed(self, _idx: int) -> None:
        fallback = self._stt_fallback_combo.currentData()
        if not fallback:
            return
        set_setting("stt_fallback_provider", fallback)
        selected = self._stt_selected_model(load_settings(), fallback)
        self._populate_stt_fallback_model_combo(fallback, selected)

    def _on_stt_fallback_model_changed(self, _idx: int) -> None:
        model = self._stt_fallback_model_combo.currentData()
        if not model:
            return
        fallback = self._stt_fallback_combo.currentData()
        # Model size/id is a global per-provider setting, shared with the
        # primary selector when both point at the same engine.
        key = "stt_groq_model" if fallback == "groq" else "stt_local_model"
        set_setting(key, model)

    def _on_provider_saved(self, _provider: str) -> None:
        # Keep duplicate (openai/google) cards across sections in sync, then
        # notify the host that the key set changed.
        self._sync_all_cards_keys()
        self.api_keys_saved.emit(load_api_keys())
        # A newly-saved key can unlock a provider — refresh the task-hierarchy
        # dropdowns so its models become selectable without reopening settings.
        if hasattr(self, "_hier_hosts"):
            self._render_hier_category("heavy")
            self._render_hier_category("medium")

    # ── Mobile token ──────────────────────────────────────────────────────

    def _update_token_mask(self) -> None:
        self._token_mask_label.setText(_mask_token(self._mobile_token))

    def _on_show_token(self) -> None:
        self._mobile_token = load_mobile_token()
        self._update_token_mask()
        if not self._mobile_token:
            return
        self._show_token_popup(
            i18n.t("settings.mobile_token_current"), self._mobile_token
        )

    def _on_regenerate_token(self) -> None:
        self._mobile_token = regenerate_mobile_token()
        self._update_token_mask()
        # The server caches the token at import, so restart it now to pick up
        # the new value immediately. The phone still needs to re-pair — see the
        # mobile-reconnection warning below the token row.
        restart_service(STARTUP_SERVICES[0])
        self._show_token_popup(
            i18n.t("settings.mobile_token_new"), self._mobile_token
        )

    def _show_token_popup(self, title: str, token: str) -> None:
        dialog = _TokenDialog(self, title, token)
        dialog.exec()

    # ── Re-translation ────────────────────────────────────────────────────

    def _retranslate_sections(self) -> None:
        self._settings_title.setText(i18n.t("settings.title"))

        self._capsule_title.setText(i18n.t("settings.capsule"))
        self._show_capsule_label.setText(i18n.t("settings.show_capsule"))
        self._autostart_label.setText(i18n.t("settings.autostart_gui"))

        self._ai_title.setText(i18n.t("settings.ai_providers"))
        self._ai_sub.setText(i18n.t("settings.ai_providers_sub"))
        self._ai_prov_label.setText(i18n.t("settings.choose_provider"))

        self._voice_title.setText(i18n.t("settings.voice_providers"))
        self._voice_sub.setText(i18n.t("settings.voice_providers_sub"))
        self._voice_prov_label.setText(i18n.t("settings.active_provider"))
        self._voice_voice_label.setText(i18n.t("settings.voice"))
        self._kokoro_tag.setText(i18n.t("tts.kokoro_desc"))
        self._tts_provider_combo.setItemText(0, i18n.t("tts.local_option"))

        self._wake_title.setText(i18n.t("settings.wake_greeting"))
        self._wake_sub.setText(i18n.t("settings.wake_greeting_sub"))
        self._wake_enabled_label.setText(i18n.t("wake.greeting_enabled"))
        self._wake_text_input.setPlaceholderText(i18n.t("wake.greeting_placeholder"))
        # If the greeting hasn't been customised, follow the UI language.
        if not (load_settings().get("wake_greeting_text") or "").strip():
            self._wake_text_input.setText(i18n.t("wake.greeting_default"))

        self._stt_title.setText(i18n.t("settings.whisper_providers"))
        self._stt_sub.setText(i18n.t("settings.whisper_providers_sub"))
        self._stt_prov_label.setText(i18n.t("settings.active_provider"))
        self._stt_model_label.setText(i18n.t("settings.model"))
        self._stt_groq_hint.setText(i18n.t("stt.groq_hint"))
        self._stt_provider_combo.setItemText(0, i18n.t("stt.local_name"))
        self._stt_provider_combo.setItemText(1, i18n.t("stt.groq_name"))
        self._stt_fallback_label.setText(i18n.t("settings.fallback_provider"))
        self._stt_fallback_model_label.setText(i18n.t("settings.model"))
        self._stt_fallback_combo.setItemText(0, i18n.t("stt.local_name"))
        self._stt_fallback_combo.setItemText(1, i18n.t("stt.groq_name"))
        self._stt_fallback_combo.setItemText(2, i18n.t("stt.fallback_none"))

        self._liveapi_title.setText(i18n.t("settings.liveapi"))
        self._liveapi_sub.setText(i18n.t("settings.liveapi_sub"))
        self._liveapi_enabled_label.setText(i18n.t("liveapi.enabled"))
        self._liveapi_prov_label.setText(i18n.t("settings.active_provider"))
        self._liveapi_model_label.setText(i18n.t("settings.model"))
        self._liveapi_docs_btn.setText(i18n.t("provider.browse"))
        self._liveapi_notice.setText(i18n.t("liveapi.notice"))

        self._hier_title.setText(i18n.t("settings.ai_hierarchy"))
        self._hier_sub.setText(i18n.t("settings.ai_hierarchy_sub"))
        self._hier_labels["heavy"].setText(i18n.t("settings.hierarchy_heavy"))
        self._hier_labels["medium"].setText(i18n.t("settings.hierarchy_medium"))
        self._hier_sublabels["heavy"].setText(i18n.t("settings.hierarchy_heavy_sub"))
        self._hier_sublabels["medium"].setText(i18n.t("settings.hierarchy_medium_sub"))
        self._hier_enabled_label.setText(i18n.t("hierarchy.enabled"))
        for _btn in self._hier_add_btns.values():
            _btn.setText("＋ " + i18n.t("settings.hierarchy_add"))
        # Rebuild the rows so the "no models" placeholder re-translates too.
        self._render_hier_category("heavy")
        self._render_hier_category("medium")

        # Scheduled Tasks (Bloc 5) — rebuild rows so the mode combos and the
        # "no tasks" placeholder re-translate too.
        self._cron_title.setText(i18n.t("settings.cron"))
        self._cron_sub.setText(i18n.t("settings.cron_sub"))
        self._cron_add_btn.setText("＋ " + i18n.t("cron.add"))
        self._render_cron()

        # Heartbeat (Bloc 5)
        self._hb_title.setText(i18n.t("settings.heartbeat"))
        self._hb_sub.setText(i18n.t("settings.heartbeat_sub"))
        self._hb_enabled_label.setText(i18n.t("heartbeat.enabled"))
        self._hb_interval_label.setText(i18n.t("heartbeat.interval"))
        for _name, _label_key, _hint_key, _ht in self._HB_METRICS:
            self._hb_metric_labels[_name].setText(i18n.t(_label_key))
            self._hb_metric_hints[_name].setText(i18n.t(_hint_key))

        # API key fallback hierarchy — title/sub/hint + rebuild the dropdown so
        # the "Select a provider…" placeholder re-translates too.
        self._apikeys_title.setText(i18n.t("settings.api_keys"))
        self._apikeys_sub.setText(i18n.t("settings.api_keys_sub"))
        self._apikeys_hint.setText(i18n.t("api_keys.no_providers"))
        self._refresh_apikeys_providers()

        self._mobile_title.setText(i18n.t("settings.mobile"))
        self._mobile_sub.setText(i18n.t("settings.mobile_sub"))
        self._mobile_token_label.setText(i18n.t("settings.mobile_token"))
        self._mobile_show_btn.setText(i18n.t("settings.show"))
        self._mobile_regen_btn.setText(i18n.t("settings.regenerate"))
        self._mobile_warn.setText(i18n.t("settings.mobile_token_warning"))

        # Startup section (autostart services)
        self._startup_title.setText(i18n.t("settings.startup"))
        self._startup_sub.setText(i18n.t("settings.startup_sub"))
        for _label, _label_key in self._startup_labels:
            _label.setText(i18n.t(_label_key))

    def _on_startup_toggle_changed(self, unit: str, toggle: QCheckBox) -> None:
        if self._startup_refresh_guard:
            return
        checked = toggle.isChecked()
        op = enable_service if checked else disable_service
        self.window().hide()
        QApplication.processEvents()
        try:
            ok = op(unit)
        finally:
            self.window().show()
        if not ok:
            self._startup_refresh_guard = True
            toggle.setChecked(not checked)
            self._startup_refresh_guard = False
        self._refresh_startup_status()

    def _refresh_startup_status(self) -> None:
        self._startup_refresh_guard = True
        for unit, dot, toggle in self._startup_rows:
            state = get_service_status(unit)
            if dot.property("state") != state:
                dot.setProperty("state", state)
                dot.style().unpolish(dot)
                dot.style().polish(dot)
            enabled = is_service_enabled(unit)
            if toggle.isChecked() != enabled:
                toggle.setChecked(enabled)
        self._startup_refresh_guard = False

    # ── Dragging ──────────────────────────────────────────────────────────
    # Dragging an empty area of the settings panel moves the whole window so
    # the panel can be repositioned anywhere on screen.

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_pos is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self.window().move(
                event.globalPosition().toPoint() - self._drag_pos
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # ── Visibility hooks ──────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._load_state()
        self._refresh_startup_status()
        self._startup_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._startup_timer.stop()

    # ── Style ─────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        # Style is applied at the MainPopup level (one stylesheet for the
        # whole window); this method exists for symmetry / future-proofing.
        return


# ── OSIA identity panel (Bloc 6) ─────────────────────────────────────────────

class _OsiaPanel(QWidget):
    """OSIA identity page — edit the assistant's *personality* and manage its
    *skill* files.

    * Personality — a single Markdown document (``~/.osia/personality.md``)
      shown in a multi-line editor with Save / Reset-to-default. It is
      prepended to the system prompt at conversation start.
    * Skills — one Markdown file per skill (``~/.osia/skills/*.md``), each with
      an enable/disable toggle, an inline editor and a delete button. The "+"
      button creates a new skill from a template. Enabled skills are appended
      to the system prompt; the enabled set persists in ``settings.json``
      (``skills_enabled``).

    Persistence lives in :mod:`osia.personality`; this panel is just the editor
    chrome. Changes take effect at the start of the next conversation, when
    :func:`osia.agent.get_system_prompt` re-reads both from disk.
    """

    def __init__(self):
        super().__init__()
        self._drag_pos: QPoint | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        # "OSIA" is a proper noun — shown verbatim like the other panel titles.
        title = QLabel("OSIA")
        title.setObjectName("panelTitle")
        header.addWidget(title)
        header.addStretch()
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        host = QWidget()
        host.setObjectName("panelHost")
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(14)
        v.addWidget(self._build_personality_section())
        v.addWidget(self._build_skills_section())
        v.addStretch(1)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        # Re-translate this panel's chrome when the UI language changes.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

        self._load_state()

    # ── Personality section ────────────────────────────────────────────────

    def _build_personality_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._pers_title = QLabel(i18n.t("osia.personality"))
        self._pers_title.setObjectName("sectionTitle")
        self._pers_sub = QLabel(i18n.t("osia.personality_sub"))
        self._pers_sub.setObjectName("sectionSub")
        self._pers_sub.setWordWrap(True)
        l.addWidget(self._pers_title)
        l.addWidget(self._pers_sub)

        self._pers_edit = QTextEdit()
        self._pers_edit.setObjectName("mdEditor")
        self._pers_edit.setAcceptRichText(False)
        self._pers_edit.setMinimumHeight(150)
        l.addWidget(self._pers_edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._pers_save_btn = QPushButton(i18n.t("provider.save"))
        self._pers_save_btn.setObjectName("saveBtn")
        self._pers_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pers_save_btn.clicked.connect(self._on_personality_save)
        self._pers_reset_btn = QPushButton(i18n.t("osia.reset_default"))
        self._pers_reset_btn.setObjectName("ghostBtn")
        self._pers_reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pers_reset_btn.clicked.connect(self._on_personality_reset)
        btn_row.addWidget(self._pers_save_btn)
        btn_row.addWidget(self._pers_reset_btn)
        btn_row.addStretch()
        l.addLayout(btn_row)
        return wrap

    def _on_personality_save(self) -> None:
        personality.save_personality(self._pers_edit.toPlainText())
        self._flash_saved(self._pers_save_btn)

    def _on_personality_reset(self) -> None:
        text = personality.reset_personality()
        self._pers_edit.setPlainText(text)
        self._flash_saved(self._pers_save_btn)

    def _flash_saved(self, btn: QPushButton) -> None:
        """Flash a save button green for 1.5s (same pattern as _ProviderCard)."""
        btn.setText(i18n.t("provider.saved"))
        btn.setProperty("saved", True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

        def restore(b=btn):
            # The button may have been destroyed by a re-render in the meantime.
            if sip is not None and sip.isdeleted(b):
                return
            b.setProperty("saved", False)
            b.setText(i18n.t("provider.save"))
            b.style().unpolish(b)
            b.style().polish(b)

        QTimer.singleShot(1500, restore)

    # ── Skills section ─────────────────────────────────────────────────────

    def _build_skills_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._skills_title = QLabel(i18n.t("osia.skills"))
        self._skills_title.setObjectName("sectionTitle")
        self._skills_sub = QLabel(i18n.t("osia.skills_sub"))
        self._skills_sub.setObjectName("sectionSub")
        self._skills_sub.setWordWrap(True)
        l.addWidget(self._skills_title)
        l.addWidget(self._skills_sub)

        host_wrap = QWidget()
        self._skills_host = QVBoxLayout(host_wrap)
        self._skills_host.setContentsMargins(0, 0, 0, 0)
        self._skills_host.setSpacing(8)
        l.addWidget(host_wrap)

        self._skills_add_btn = QPushButton("＋ " + i18n.t("osia.skill_add"))
        self._skills_add_btn.setObjectName("primaryBtn")
        self._skills_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skills_add_btn.clicked.connect(self._on_skill_add)
        l.addWidget(self._skills_add_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return wrap

    def _render_skills(self) -> None:
        host = self._skills_host
        while host.count():
            item = host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        skills = personality.list_skills()
        if not skills:
            empty = QLabel(i18n.t("osia.skills_empty"))
            empty.setObjectName("providerSub")
            empty.setWordWrap(True)
            host.addWidget(empty)
            return
        for name in skills:
            host.addWidget(self._build_skill_row(name))

    def _build_skill_row(self, name: str) -> QWidget:
        row = QFrame()
        row.setObjectName("providerCard")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        # Row A — filename, enable/disable toggle, edit, delete.
        top = QHBoxLayout()
        top.setSpacing(6)
        label = QLabel(name)
        label.setObjectName("providerName")
        enable = QCheckBox()
        enable.setObjectName("smartToggle")
        enable.setChecked(personality.is_skill_enabled(name))
        enable.setToolTip(i18n.t("osia.skill_toggle"))
        enable.stateChanged.connect(
            lambda _s, n=name, c=enable:
                personality.set_skill_enabled(n, c.isChecked())
        )
        edit_btn = QPushButton("✎")
        edit_btn.setObjectName("ghostBtn")
        edit_btn.setFixedSize(28, 28)
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setToolTip(i18n.t("osia.skill_edit"))
        rm = QPushButton("✕")
        rm.setObjectName("convDel")
        rm.setFixedSize(28, 28)
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.setToolTip(i18n.t("osia.skill_delete"))
        rm.clicked.connect(lambda _=False, n=name: self._on_skill_delete(n))
        top.addWidget(label, 1)
        top.addWidget(enable)
        top.addWidget(edit_btn)
        top.addWidget(rm)
        v.addLayout(top)

        # Row B — inline editor, hidden until the ✎ button is clicked.
        editor_wrap = QWidget()
        ev = QVBoxLayout(editor_wrap)
        ev.setContentsMargins(0, 4, 0, 0)
        ev.setSpacing(6)
        editor = QTextEdit()
        editor.setObjectName("mdEditor")
        editor.setAcceptRichText(False)
        editor.setMinimumHeight(140)
        editor.setPlainText(personality.read_skill(name))
        ev.addWidget(editor)
        ed_btns = QHBoxLayout()
        ed_btns.setSpacing(8)
        save_btn = QPushButton(i18n.t("provider.save"))
        save_btn.setObjectName("saveBtn")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(
            lambda _=False, n=name, e=editor, b=save_btn:
                self._on_skill_save(n, e, b)
        )
        close_btn = QPushButton(i18n.t("osia.skill_close"))
        close_btn.setObjectName("ghostBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(
            lambda _=False, w=editor_wrap: w.setVisible(False)
        )
        ed_btns.addWidget(save_btn)
        ed_btns.addWidget(close_btn)
        ed_btns.addStretch()
        ev.addLayout(ed_btns)
        editor_wrap.setVisible(False)
        v.addWidget(editor_wrap)

        edit_btn.clicked.connect(
            lambda _=False, w=editor_wrap, e=editor, n=name:
                self._toggle_skill_editor(w, e, n)
        )
        return row

    def _toggle_skill_editor(self, wrap: QWidget, editor: QTextEdit,
                             name: str) -> None:
        if wrap.isVisible():
            wrap.setVisible(False)
        else:
            # Reload from disk so the editor reflects any external change.
            editor.setPlainText(personality.read_skill(name))
            wrap.setVisible(True)

    def _on_skill_save(self, name: str, editor: QTextEdit,
                       btn: QPushButton) -> None:
        personality.write_skill(name, editor.toPlainText())
        self._flash_saved(btn)

    def _on_skill_add(self) -> None:
        name, ok = QInputDialog.getText(
            self, i18n.t("osia.skill_new_title"), i18n.t("osia.skill_new_prompt")
        )
        if not ok:
            return
        if personality.create_skill(name):
            self._render_skills()

    def _on_skill_delete(self, name: str) -> None:
        personality.delete_skill(name)
        self._render_skills()

    # ── State ──────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        self._pers_edit.setPlainText(personality.load_personality())
        self._render_skills()

    def refresh(self) -> None:
        """Reload personality + skills from disk — called when the tab opens."""
        self._load_state()

    def _retranslate(self) -> None:
        self._pers_title.setText(i18n.t("osia.personality"))
        self._pers_sub.setText(i18n.t("osia.personality_sub"))
        if not self._pers_save_btn.property("saved"):
            self._pers_save_btn.setText(i18n.t("provider.save"))
        self._pers_reset_btn.setText(i18n.t("osia.reset_default"))
        self._skills_title.setText(i18n.t("osia.skills"))
        self._skills_sub.setText(i18n.t("osia.skills_sub"))
        self._skills_add_btn.setText("＋ " + i18n.t("osia.skill_add"))
        # Deliberately do NOT rebuild the skill rows here: _render_skills() is
        # destructive (it tears down every row, collapsing any open inline
        # editor and discarding unsaved edits). The language selector lives on
        # the Settings page, so these rows are never visible at the moment the
        # language changes, and they re-translate on the next refresh() (every
        # time the OSIA tab is opened).

    # ── Dragging (mirrors _SettingsPanel) ──────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_pos is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self.window().move(
                event.globalPosition().toPoint() - self._drag_pos
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


# ── Bottom-nav button ────────────────────────────────────────────────────────

class _NavButton(QPushButton):
    """A bottom-nav button that paints a crisp vector icon.

    Qt's emoji rendering is inconsistent across platforms, so each nav entry
    draws its own glyph via ``QPainterPath``:

    * ``conv``     — a rounded speech bubble (Conversations)
    * ``models``   — a four-point sparkle/star (Models)
    * ``settings`` — a gear (Settings)
    * ``osia``     — a person/identity silhouette (OSIA personality + skills)

    The active tab gets a rounded violet pill behind the icon and the icon
    itself switches to the violet accent; hover shows a softer violet wash.
    """

    ICON_PX = 33

    def __init__(self, kind: str, tooltip: str):
        super().__init__()
        self._kind = kind
        # i18n key for the tooltip, set by the owner; None for proper nouns
        # (e.g. "OSIA") that are shown verbatim and never translated.
        self._tip_key: str | None = None
        self.setObjectName("navBtn")
        self.setCheckable(True)
        # 56px (was 48) so the painted glyphs — notably the speech-bubble tail,
        # which reaches ~0.22*ICON_PX below the bubble body — have vertical room
        # and no longer clip against the button's bottom edge.
        self.setFixedSize(52, 56)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        active = self.isChecked()

        # Rounded pill background — a subtle violet wash when active (no border
        # line), a softer one on hover.
        if active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(196, 181, 245, 31))  # rgba(196,181,245,0.12)
            p.drawRoundedRect(rect, 10, 10)
        elif self._hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(196, 181, 245, 36))
            p.drawRoundedRect(rect, 12, 12)

        # Icon colour: violet accent when active, dim white otherwise.
        color = ACCENT if active else QColor(245, 245, 250, 200)

        cx = self.width() / 2
        cy = self.height() / 2
        s = self.ICON_PX
        if self._kind == "conv":
            self._paint_bubble(p, cx, cy, s, color)
        elif self._kind == "models":
            self._paint_sparkle(p, cx, cy, s, color)
        elif self._kind == "settings":
            self._paint_gear(p, cx, cy, s, color)
        elif self._kind == "osia":
            self._paint_person(p, cx, cy, s, color)
        p.end()

    # ── Icon glyphs ───────────────────────────────────────────────────────

    def _paint_bubble(self, p, cx, cy, s, color) -> None:
        """Rounded speech bubble with a little tail at the bottom-left."""
        w = s
        h = s * 0.82
        body = QRectF(cx - w / 2, cy - h / 2 - 1, w, h)
        path = QPainterPath()
        path.addRoundedRect(body, 6, 6)
        # Tail
        tail = QPainterPath()
        tail.moveTo(cx - w * 0.18, body.bottom() - 1)
        tail.lineTo(cx - w * 0.34, body.bottom() + s * 0.22)
        tail.lineTo(cx + w * 0.02, body.bottom() - 1)
        tail.closeSubpath()
        path = path.united(tail)
        p.setPen(QPen(color, 2.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _paint_sparkle(self, p, cx, cy, s, color) -> None:
        """A four-point sparkle/star (concave diamond)."""
        r = s / 2
        waist = r * 0.32      # how far the concave mid-points pull in
        path = QPainterPath()
        path.moveTo(cx, cy - r)                 # top
        path.lineTo(cx + waist, cy - waist)
        path.lineTo(cx + r, cy)                 # right
        path.lineTo(cx + waist, cy + waist)
        path.lineTo(cx, cy + r)                 # bottom
        path.lineTo(cx - waist, cy + waist)
        path.lineTo(cx - r, cy)                 # left
        path.lineTo(cx - waist, cy - waist)
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(path)

    def _paint_gear(self, p, cx, cy, s, color) -> None:
        """A gear — toothed ring with a hollow centre."""
        outer = s / 2
        inner = outer * 0.62
        teeth = 8
        path = QPainterPath()
        steps = teeth * 2
        for k in range(steps + 1):
            ang = k * math.pi / teeth
            rad = outer if (k % 2 == 0) else inner
            x = cx + rad * math.cos(ang)
            y = cy + rad * math.sin(ang)
            if k == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        path.closeSubpath()
        # Hollow hub
        hub = outer * 0.40
        hole = QPainterPath()
        hole.addEllipse(QPointF(cx, cy), hub, hub)
        path = path.subtracted(hole)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(path)

    def _paint_person(self, p, cx, cy, s, color) -> None:
        """A person/identity silhouette — a head circle above a shoulders dome."""
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        # Head.
        head_r = s * 0.21
        head_cy = cy - s * 0.24
        p.drawEllipse(QPointF(cx, head_cy), head_r, head_r)
        # Shoulders — the top half of an ellipse sitting just below the head.
        sw = s * 0.80          # shoulder width
        sh = s * 0.64          # full ellipse height (only the top half is drawn)
        shoulders_cy = cy + s * 0.28
        rect = QRectF(cx - sw / 2, shoulders_cy - sh / 2, sw, sh)
        path = QPainterPath()
        path.moveTo(rect.left(), shoulders_cy)
        path.arcTo(rect, 180, -180)   # left → over the top → right
        path.closeSubpath()           # flat bottom
        p.drawPath(path)


# ── MainPopup ───────────────────────────────────────────────────────────────

class MainPopup(QWidget):
    """Floating glass panel anchored just below the Dynamic Island."""

    send_text = pyqtSignal(str)
    pick_file = pyqtSignal(str)
    new_conversation = pyqtSignal()
    open_conversation = pyqtSignal(str)
    delete_conversation = pyqtSignal(str)
    model_changed = pyqtSignal(str)
    api_keys_saved = pyqtSignal(dict)
    # Re-emitted from the settings panel: the floating-capsule visibility toggle
    # (WOR-22). The host wires it to the Dynamic Island's set_capsule_visible.
    capsule_visibility_changed = pyqtSignal(bool)

    # Index of the chat page in the QStackedWidget.
    PAGE_CHAT = 0
    PAGE_CONVS = 1
    PAGE_MODELS = 2
    PAGE_SETTINGS = 3
    PAGE_OSIA = 4

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(POPUP_W, POPUP_H)

        # The Dynamic Island capsule the popup anchors to (set_anchor) —
        # when set, the popup opens just below the capsule's CURRENT
        # position instead of a fixed screen coordinate.
        self._anchor: QWidget | None = None
        # Drag state — presses that no child widget consumed land on the
        # popup itself and move the whole window.
        self._drag_offset = None

        # Soft shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(60)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 220))
        self.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(8)

        # Header — OSIA logo on the left, close button on the right. The logo
        # alone represents OSIA, so no separate "OSIA" wordmark is shown.
        header = QHBoxLayout()
        header.setSpacing(8)

        logo = QLabel()
        logo.setObjectName("popupLogo")
        logo_pix = QPixmap(LOGO_PATH)
        if not logo_pix.isNull():
            logo.setPixmap(
                logo_pix.scaledToHeight(
                    44, Qt.TransformationMode.SmoothTransformation
                )
            )

        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(26, 26)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)

        header.addWidget(logo)
        header.addStretch()
        header.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header)

        # Central stack
        self._stack = QStackedWidget()
        self._chat = _ChatPage()
        self._chat.send_text.connect(self.send_text.emit)
        self._chat.pick_file.connect(self.pick_file.emit)

        self._convs = _ConversationsPanel()
        self._convs.new_conversation.connect(self._on_new_conv)
        self._convs.open_conversation.connect(self._on_open_conv)
        self._convs.delete_conversation.connect(self.delete_conversation.emit)

        self._models = _ModelPanel()
        self._models.model_changed.connect(self._on_model_selected)

        self._settings = _SettingsPanel()
        self._settings.api_keys_saved.connect(self.api_keys_saved.emit)
        self._settings.capsule_visibility_changed.connect(
            self.capsule_visibility_changed.emit
        )

        self._osia = _OsiaPanel()

        self._stack.addWidget(self._chat)
        self._stack.addWidget(self._convs)
        self._stack.addWidget(self._models)
        self._stack.addWidget(self._settings)
        self._stack.addWidget(self._osia)
        outer.addWidget(self._stack, 1)

        # Bottom nav
        nav = QHBoxLayout()
        nav.setSpacing(12)
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        self._nav_buttons: dict[int, QPushButton] = {}
        # "OSIA" is a proper noun (tip_key=None) — shown verbatim; the others
        # carry an i18n key so their tooltips re-translate on language change.
        for page_idx, kind, tip_key in [
            (self.PAGE_CONVS,   "conv",     "nav.conversations"),
            (self.PAGE_MODELS,  "models",   "nav.models"),
            (self.PAGE_SETTINGS,"settings", "nav.settings"),
            (self.PAGE_OSIA,    "osia",     None),
        ]:
            tooltip = i18n.t(tip_key) if tip_key else "OSIA"
            btn = _NavButton(kind, tooltip)
            btn._tip_key = tip_key
            btn.clicked.connect(
                lambda _checked=False, idx=page_idx: self._on_nav_clicked(idx)
            )
            self._nav_group.addButton(btn, page_idx)
            self._nav_buttons[page_idx] = btn

        # Keep nav tooltips translated on language change.
        i18n.add_listener(self._retranslate_nav)
        self.destroyed.connect(
            lambda: i18n.remove_listener(self._retranslate_nav)
        )

        nav.addStretch()
        for btn in self._nav_buttons.values():
            nav.addWidget(btn)
        nav.addStretch()
        outer.addLayout(nav)

        self._apply_style()
        self._stack.setCurrentIndex(self.PAGE_CHAT)

    # ── Window placement ──────────────────────────────────────────────────

    def set_anchor(self, widget: QWidget) -> None:
        """Set the widget (the Dynamic Island capsule) the popup opens under.

        The capsule is draggable, so its geometry is re-read on every
        :meth:`toggle` — the popup always opens below wherever the capsule
        currently is.
        """
        self._anchor = widget

    def anchor_below_island(self, island_geom_bottom_y: int = 60) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()

        if self._anchor is not None:
            # Follow the capsule's current position: horizontally centred
            # under it, just below it — on the capsule's screen. When there
            # isn't enough room below (capsule dragged near the bottom), the
            # popup flips above the capsule instead of covering it; the final
            # clamp keeps it fully visible in every case.
            island_geo = self._anchor.frameGeometry()
            island_screen = self._anchor.screen()
            if island_screen is not None:
                geo = island_screen.availableGeometry()
            x = island_geo.center().x() - self.width() // 2
            y = island_geo.bottom() + 10
            if (y + self.height() > geo.y() + geo.height()
                    and island_geo.top() - 10 - self.height() >= geo.y()):
                y = island_geo.top() - 10 - self.height()
            x = max(geo.x(), min(x, geo.x() + geo.width() - self.width()))
            y = max(geo.y(), min(y, geo.y() + geo.height() - self.height()))
            self.move(x, y)
            return

        # No anchor — legacy fixed placement at the top-centre of the screen.
        x = geo.x() + (geo.width() - self.width()) // 2
        y = geo.y() + max(island_geom_bottom_y, 60)
        self.move(x, y)

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.anchor_below_island()
            self.show()
            self.raise_()
            self.activateWindow()

    # ── Public API ────────────────────────────────────────────────────────

    def show_chat(self) -> None:
        self._stack.setCurrentIndex(self.PAGE_CHAT)
        for b in self._nav_buttons.values():
            b.setChecked(False)

    def set_messages(self, messages: list[dict]) -> None:
        self._chat.set_messages(messages)

    def reset_conversation(self) -> None:
        """Clear the chat view back to its empty state and show the chat page.

        Display-only counterpart to the "Nouveau chat" action: used when the
        backend (agent history + active conversation) has already been reset
        elsewhere — e.g. the "nouveau chat" voice command (WOR-10).
        """
        self._chat.set_messages([])
        self.show_chat()

    def append_message(self, role: str, text: str) -> None:
        self._chat.append_message(role, text)

    def refresh_conversations(self, active_id: str | None) -> None:
        self._convs.set_active(active_id)

    def set_current_model(self, model_id: str | None) -> None:
        self._models.set_current(model_id)

    def set_remote_models(self, remote: list) -> None:
        self._models.set_remote_models(remote)

    # ── Internal handlers ─────────────────────────────────────────────────

    def _retranslate_nav(self) -> None:
        for btn in self._nav_buttons.values():
            tip_key = getattr(btn, "_tip_key", None)
            if tip_key:
                btn.setToolTip(i18n.t(tip_key))

    def _on_nav_clicked(self, page_idx: int) -> None:
        # Clicking the active nav button returns to the chat.
        if self._stack.currentIndex() == page_idx:
            self.show_chat()
            return
        self._stack.setCurrentIndex(page_idx)
        if page_idx == self.PAGE_CONVS:
            self._convs.refresh()
        elif page_idx == self.PAGE_MODELS:
            self._models.refresh()
        elif page_idx == self.PAGE_OSIA:
            self._osia.refresh()

    def _on_new_conv(self) -> None:
        self.new_conversation.emit()
        self.show_chat()

    def _on_open_conv(self, conv_id: str) -> None:
        self.open_conversation.emit(conv_id)
        self.show_chat()

    def _on_model_selected(self, model_id: str) -> None:
        self.model_changed.emit(model_id)
        # Show the new selection state immediately.
        self._models.set_current(model_id)

    # ── Dragging ──────────────────────────────────────────────────────────
    # Buttons, inputs and list rows accept their own mouse presses, so any
    # press that reaches the popup itself is on an "empty" area — dragging
    # there moves the whole window (same pattern as _SettingsPanel).

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    # ── Painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(
            rect.x() + 0.5, rect.y() + 0.5,
            rect.width(), rect.height(),
            RADIUS, RADIUS,
        )
        painter.fillPath(path, BG_COLOR)
        # A second, slightly lighter wash layered on top fakes a frosted-glass
        # blur — the panel reads as semi-transparent glass rather than a flat
        # dark fill.
        painter.fillPath(path, BG_BLUR_LAYER)
        painter.setPen(BORDER_COLOR)
        painter.drawPath(path)
        painter.end()

    # ── Style ─────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QWidget {
                color: #e8e6f0;
                font-family: -apple-system, "Segoe UI", "SF Pro Text",
                             "Helvetica Neue", sans-serif;
            }

            QPushButton#closeBtn {
                background: rgba(196, 181, 245, 0.06);
                color: rgba(232, 230, 240, 0.7);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 13px;
                font-size: 12px;
            }
            QPushButton#closeBtn:hover {
                background: rgba(196, 181, 245, 0.16);
                color: #e8e6f0;
            }

            /* Panels ─────────────────────────────────────────────────── */
            QLabel#panelTitle {
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#panelSub {
                font-size: 11px;
                color: #9d9aad;
            }
            QLabel#panelEmpty {
                color: #9d9aad;
                font-size: 12px;
                padding: 30px 10px;
            }

            QScrollArea#chatScroll, QScrollArea#panelScroll,
            QWidget#chatHost, QWidget#panelHost {
                background: transparent;
                border: none;
            }
            /* The scroll-area viewport + its host pick up an opaque grey
               palette background AND a 1px frame by default — force every
               layer (viewport + host) back to transparent with no border so
               panels sit on the app's dark #0d0b12 glass and no stray rule
               shows through when the list is empty and no rows cover it. */
            QScrollArea#chatScroll > QWidget,
            QScrollArea#panelScroll > QWidget,
            QScrollArea#chatScroll > QWidget > QWidget,
            QScrollArea#panelScroll > QWidget > QWidget {
                background: transparent;
                border: none;
            }

            /* Chat ───────────────────────────────────────────────────── */
            QLabel#chatEmpty {
                color: #9d9aad;
                font-size: 12px;
                padding: 40px 12px;
            }
            QFrame#msgBubble {
                border-radius: 16px;
                padding: 0px;
            }
            QFrame#msgBubble[role="user"] {
                background: rgba(196, 181, 245, 0.20);
                border: 1px solid rgba(196, 181, 245, 0.30);
            }
            QFrame#msgBubble[role="assistant"] {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.12);
            }
            QLabel#msgText {
                color: #e8e6f0;
                font-size: 12px;
                background: transparent;
            }

            QFrame#inputWrap {
                background: #13111a;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 16px;
            }
            QFrame#inputWrap:hover {
                border: 1px solid rgba(196, 181, 245, 0.34);
            }
            QLineEdit#chatInput {
                background: transparent;
                border: none;
                color: #e8e6f0;
                font-size: 12px;
                padding: 4px 2px;
            }
            QPushButton#iconBtn {
                background: rgba(196, 181, 245, 0.06);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 17px;
                color: rgba(232, 230, 240, 0.8);
                font-size: 20px;
                font-weight: 600;
            }
            QPushButton#iconBtn:hover {
                background: rgba(196, 181, 245, 0.20);
                color: #e8e6f0;
            }
            QPushButton#sendBtn {
                background: rgba(196, 181, 245, 0.85);
                border: none;
                border-radius: 17px;
                color: #0d0b12;
                font-size: 20px;
                font-weight: 700;
            }
            QPushButton#sendBtn:hover {
                background: rgba(196, 181, 245, 1.0);
            }

            /* Bottom nav (icons are painted by _NavButton) ───────────── */
            QPushButton#navBtn {
                background: transparent;
                border: none;
            }

            /* Conversations list ────────────────────────────────────── */
            QFrame#convRow {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 12px;
            }
            QFrame#convRow:hover {
                border: 1px solid rgba(196, 181, 245, 0.35);
                background: #13111a;
            }
            QFrame#convRow[active="true"] {
                background: rgba(196, 181, 245, 0.16);
                border: 1px solid rgba(196, 181, 245, 0.5);
                border-left: 3px solid #c4b5f5;
            }
            QLabel#convTitle {
                font-size: 13px;
                font-weight: 600;
                padding-left: 2px;
            }
            QLabel#convSub {
                font-size: 11px;
                color: #9d9aad;
            }
            QPushButton#convOpen {
                background: rgba(196, 181, 245, 0.20);
                border: 1px solid rgba(196, 181, 245, 0.40);
                color: #e8e6f0;
                border-radius: 10px;
                padding: 8px 16px;
                font-size: 11px;
            }
            QPushButton#convOpen:hover {
                background: rgba(196, 181, 245, 0.35);
            }
            QPushButton#convDel {
                background: rgba(196, 181, 245, 0.04);
                border: 1px solid rgba(196, 181, 245, 0.12);
                color: rgba(232, 230, 240, 0.6);
                border-radius: 13px;
                font-size: 11px;
            }
            QPushButton#convDel:hover {
                background: rgba(220, 80, 80, 0.25);
                border: 1px solid rgba(220, 80, 80, 0.6);
                color: #ffffff;
            }

            /* Model panel — provider grid ───────────────────────────── */
            QPushButton#backBtn {
                background: rgba(196, 181, 245, 0.05);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 8px 16px;
                font-size: 12px;
                color: rgba(232, 230, 240, 0.85);
            }
            QPushButton#backBtn:hover {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #e8e6f0;
            }
            QFrame#provGridCard {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 12px;
            }
            QFrame#provGridCard:hover {
                border: 1px solid rgba(196, 181, 245, 0.45);
                background: #13111a;
            }
            QLabel#provGridName {
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#provGridCount {
                font-size: 11px;
                color: #9d9aad;
            }

            /* Model list ────────────────────────────────────────────── */
            QLabel#modelGroup {
                font-size: 11px;
                font-weight: 600;
                color: rgba(196, 181, 245, 0.85);
                letter-spacing: 1.5px;
                text-transform: uppercase;
                padding: 8px 2px 2px 2px;
            }
            QFrame#modelRow {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 12px;
            }
            QFrame#modelRow:hover {
                border: 1px solid rgba(196, 181, 245, 0.35);
                background: #13111a;
            }
            QFrame#modelRow[active="true"] {
                background: rgba(196, 181, 245, 0.16);
                border: 1px solid rgba(196, 181, 245, 0.5);
                border-left: 3px solid #c4b5f5;
            }
            QLabel#modelProv {
                color: rgba(196, 181, 245, 0.85);
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
            }
            QLabel#modelName {
                font-size: 13px;
                font-weight: 500;
            }
            QLabel#modelSub {
                font-size: 11px;
                color: #9d9aad;
            }
            QPushButton#modelUse {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.40);
                color: #e8e6f0;
                border-radius: 10px;
                padding: 8px 16px;
                font-size: 11px;
            }
            QPushButton#modelUse:hover {
                background: rgba(196, 181, 245, 0.35);
            }
            QPushButton#modelUse:disabled {
                background: rgba(196, 181, 245, 0.22);
                border: 1px solid rgba(196, 181, 245, 0.5);
                color: rgba(232, 230, 240, 0.7);
            }

            /* Settings sections ─────────────────────────────────────── */
            QFrame#section {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 12px;
            }
            QFrame#section:hover {
                border: 1px solid rgba(196, 181, 245, 0.32);
            }
            QLabel#sectionTitle {
                font-size: 13px;
                font-weight: 600;
                background: rgba(196, 181, 245, 0.06);
                border-radius: 10px;
                padding: 5px 10px;
            }
            QLabel#sectionSub {
                font-size: 11px;
                color: #9d9aad;
            }
            QLabel#fieldLabel {
                font-size: 12px;
                color: #9d9aad;
            }
            QLabel#startupLabel {
                font-size: 12px;
                color: rgba(232, 230, 240, 0.85);
            }
            QLabel#statusDot {
                border-radius: 5px;
                background: rgba(180, 180, 180, 0.4);
            }
            QLabel#statusDot[state="running"] {
                background: #c4b5f5;
            }
            QLabel#statusDot[state="stopped"] {
                background: rgba(180, 180, 180, 0.4);
            }
            QLabel#statusDot[state="error"] {
                background: rgba(196, 181, 245, 0.55);
            }

            /* Provider card (compact configuration row) ─────────────── */
            QFrame#providerCard {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 12px;
            }
            QFrame#providerCard:hover {
                border: 1px solid rgba(196, 181, 245, 0.35);
            }
            QLabel#providerName {
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#providerSub {
                font-size: 11px;
                color: #9d9aad;
            }
            QLabel#infoBadge {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid #c4b5f5;
                border-radius: 7px;
                color: #c4b5f5;
                font-size: 9px;
                font-weight: 700;
            }

            /* Mobile token + reveal dialog ──────────────────────────────── */
            QLabel#tokenMask {
                font-family: "DejaVu Sans Mono", "SF Mono", monospace;
                font-size: 13px;
                color: #e8e6f0;
                letter-spacing: 1px;
            }
            QLabel#warnLabel {
                color: #e0a23c;
                font-size: 11px;
            }
            /* Live API (Bloc 4) — violet info box: STT + TTS bypassed notice */
            QLabel#liveApiNotice {
                background: rgba(196, 181, 245, 0.14);
                border: 1px solid rgba(196, 181, 245, 0.30);
                border-radius: 10px;
                padding: 8px 10px;
                color: #c4b5f5;
                font-size: 11px;
            }
            QFrame#dialogCard {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.30);
                border-radius: 14px;
            }
            QLabel#dialogTitle {
                font-size: 14px;
                font-weight: 600;
                color: #e8e6f0;
            }
            /* Conversation pill on a cron row + the picker popover rows */
            QLabel#convPill {
                background: rgba(196, 181, 245, 0.14);
                border: 1px solid rgba(196, 181, 245, 0.30);
                border-radius: 10px;
                padding: 2px 9px;
                color: #c4b5f5;
                font-size: 10px;
            }
            QScrollArea#convScroll {
                background: transparent;
                border: none;
            }
            QPushButton#convPick {
                background: rgba(196, 181, 245, 0.04);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 8px 16px;
                color: #e8e6f0;
                font-size: 12px;
                text-align: left;
            }
            QPushButton#convPick:hover {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #e8e6f0;
            }

            QPushButton#ghostBtn {
                background: rgba(196, 181, 245, 0.05);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 8px 16px;
                font-size: 11px;
                color: rgba(232, 230, 240, 0.85);
            }
            QPushButton#ghostBtn:hover {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #e8e6f0;
            }
            QPushButton#primaryBtn {
                background: rgba(196, 181, 245, 0.22);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #e8e6f0;
                border-radius: 10px;
                padding: 8px 16px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton#primaryBtn:hover {
                background: rgba(196, 181, 245, 0.36);
            }
            QPushButton#linkBtn {
                background: transparent;
                border: none;
                color: #9d9aad;
                font-size: 11px;
                text-decoration: underline;
            }
            QPushButton#linkBtn:hover {
                color: #e8e6f0;
            }

            QLineEdit#keyInput {
                background: #13111a;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 6px 10px;
                color: #e8e6f0;
                font-size: 12px;
            }
            QLineEdit#keyInput:focus {
                border: 1px solid rgba(196, 181, 245, 0.55);
            }
            /* Multi-line Markdown editor (OSIA personality + skills) ──── */
            QTextEdit#mdEditor {
                background: #13111a;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 6px 10px;
                color: #e8e6f0;
                font-family: "DejaVu Sans Mono", "SF Mono", monospace;
                font-size: 12px;
                selection-background-color: rgba(196, 181, 245, 0.30);
            }
            QTextEdit#mdEditor:focus {
                border: 1px solid rgba(196, 181, 245, 0.55);
            }
            QPushButton#saveBtn {
                background: rgba(196, 181, 245, 0.08);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 8px 18px;
                color: #e8e6f0;
                font-size: 12px;
            }
            QPushButton#saveBtn:hover {
                background: rgba(196, 181, 245, 0.30);
                border: 1px solid rgba(196, 181, 245, 0.55);
            }
            QPushButton#saveBtn[saved="true"] {
                background: rgba(196, 181, 245, 0.35);
                border: 1px solid rgba(196, 181, 245, 0.85);
                color: #e8e6f0;
            }

            /* Voice combos ──────────────────────────────────────────── */
            QComboBox#voiceCombo {
                background: #13111a;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                padding: 6px 10px;
                color: #e8e6f0;
                font-size: 12px;
            }
            QComboBox#voiceCombo::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox#voiceCombo::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid #9d9aad;
                margin-right: 6px;
            }
            QComboBox#voiceCombo QAbstractItemView {
                background: #13111a;
                color: #e8e6f0;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                selection-background-color: rgba(196, 181, 245, 0.30);
                font-size: 12px;
            }

            /* Smart-toggle ──────────────────────────────────────────── */
            QCheckBox#smartToggle::indicator {
                width: 36px;
                height: 20px;
                border-radius: 10px;
                background: rgba(196, 181, 245, 0.15);
                border: 1px solid rgba(196, 181, 245, 0.12);
            }
            QCheckBox#smartToggle::indicator:checked {
                background: rgba(196, 181, 245, 0.85);
                border: 1px solid rgba(196, 181, 245, 1.0);
            }

            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(196, 181, 245, 0.18);
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(196, 181, 245, 0.30);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """)

    # ── Keys ──────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


# ── Self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    app = QApplication(sys.argv)
    p = MainPopup()
    p.anchor_below_island()
    p.show()
    p.append_message("user", "Bonjour OSIA")
    p.append_message("assistant", "Salut ! Comment puis-je t'aider ?")
    sys.exit(app.exec())
