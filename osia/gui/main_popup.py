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
import webbrowser
from datetime import datetime
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
    QButtonGroup, QGridLayout, QDialog,
)

from osia import conversations
from osia.agent import strip_tool_artifacts
from osia.models import (
    PROVIDERS, get_all_models,
)
from osia.config import (
    load_api_keys, save_api_keys, load_settings, set_setting, set_api_key,
    load_mobile_token, regenerate_mobile_token,
)
from osia.utils.systemd_manager import (
    is_service_enabled, enable_service, disable_service, get_service_status,
    restart_service,
)
from osia.utils.assets import LOGO_PATH
from osia import i18n

# sip lets us detect widgets whose C++ side has already been destroyed
# (accessing them raises RuntimeError otherwise). It ships with PyQt6 but
# guard the import so a missing module never breaks the GUI.
try:
    from PyQt6 import sip
except ImportError:
    sip = None


# ── Visual constants ────────────────────────────────────────────────────────

POPUP_W = 480
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
        return dt.strftime("%d %b %H:%M")
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
        self._file_btn.setToolTip("Attach a file")
        self._file_btn.clicked.connect(self._on_pick_file)

        self._input = QLineEdit()
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText("Message OSIA…")
        self._input.returnPressed.connect(self._on_send)

        self._send_btn = QPushButton("➤")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setFixedSize(34, 34)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setToolTip("Send")
        self._send_btn.clicked.connect(self._on_send)

        row.addWidget(self._file_btn)
        row.addWidget(self._input, 1)
        row.addWidget(self._send_btn)
        outer.addWidget(input_wrap)

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
        label = QLabel("Say “OSIA” or type below to start.")
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
        path, _ = QFileDialog.getOpenFileName(self, "Attach a file")
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
        self._full_title = conv.get("title") or "New conversation"
        title = QLabel(self._full_title)
        title.setObjectName("convTitle")
        title.setWordWrap(False)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # Width is ignored so a long title never widens the row; it is elided
        # with an ellipsis (…) in resizeEvent instead of overflowing.
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._title_label = title
        sub = QLabel(
            f"{conv.get('message_count', 0)} msg · "
            f"{_format_time(conv.get('updated_at'))}"
        )
        sub.setObjectName("convSub")
        title_col.addWidget(title)
        title_col.addWidget(sub)

        open_btn = QPushButton("Open")
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
        title = QLabel("Conversations")
        title.setObjectName("panelTitle")
        new_btn = QPushButton("＋ New chat")
        new_btn.setObjectName("primaryBtn")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.clicked.connect(self.new_conversation.emit)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(new_btn)
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

        btn = QPushButton("✓ Active" if is_active else "Use")
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
            f"{model_count} model{'s' if model_count != 1 else ''}"
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
        self._back_btn = QPushButton("←  Back")
        self._back_btn.setObjectName("backBtn")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self._show_grid)
        self._back_btn.hide()
        self._title = QLabel("Models")
        self._title.setObjectName("panelTitle")
        header.addWidget(self._back_btn)
        header.addWidget(self._title)
        header.addStretch()
        outer.addLayout(header)

        self._sub = QLabel("Choose a provider to see its models.")
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
        self._title.setText("Models")
        self._sub.setText("Choose a provider to see its models.")

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
        self._sub.setText("Pick the model OSIA will use for new messages.")

        models = [
            m for m in get_all_models(remote_models=self._remote)
            if m["provider"] == provider
        ]
        models.sort(key=lambda m: m.get("label", ""))

        if not models:
            empty = QLabel("No models available for this provider.")
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
        self._drag_pos: QPoint | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("panelTitle")
        min_btn = QPushButton("−")
        min_btn.setObjectName("minBtn")
        min_btn.setFixedSize(26, 26)
        min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        min_btn.setToolTip("Minimize")
        min_btn.clicked.connect(self.hide)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(min_btn)
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
        v.addWidget(self._build_ai_providers_section())     # Section 1
        v.addWidget(self._build_voice_section())            # Section 2 (TTS)
        v.addWidget(self._build_stt_section())              # Section 3 (STT)
        v.addWidget(self._build_liveapi_section())          # Section 4 (Live API)
        v.addWidget(self._build_ai_hierarchy_section())     # AI Task Hierarchy
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

        # Keep this section's own labels translated on language change.
        i18n.add_listener(self._retranslate_language_section)
        self.destroyed.connect(
            lambda: i18n.remove_listener(self._retranslate_language_section)
        )
        return wrap

    def _retranslate_language_section(self) -> None:
        self._language_title.setText(i18n.t("settings.language"))
        self._language_sub.setText(i18n.t("settings.language_sub"))

    def _on_language_changed(self, _idx: int) -> None:
        code = self._language_combo.currentData()
        if code:
            i18n.set_language(code)

    def _build_startup_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        title = QLabel("Démarrage")
        title.setObjectName("sectionTitle")
        sub = QLabel("Lancer le serveur mobile et le tunnel Cloudflare au démarrage.")
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)
        l.addWidget(title)
        l.addWidget(sub)

        rows = [
            (STARTUP_SERVICES[0], "Serveur mobile"),
            (STARTUP_SERVICES[1], "Tunnel Cloudflare"),
        ]
        for unit, display in rows:
            row = QHBoxLayout()
            label = QLabel(display)
            label.setObjectName("startupLabel")
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
        self._voice_sub = QLabel(i18n.t("settings.voice_providers_sub"))
        self._voice_sub.setObjectName("sectionSub")
        self._voice_sub.setWordWrap(True)
        l.addWidget(self._voice_title)
        l.addWidget(self._voice_sub)

        # Active provider selector.
        prov_row = QHBoxLayout()
        self._voice_prov_label = QLabel(i18n.t("settings.active_provider"))
        self._voice_prov_label.setObjectName("fieldLabel")
        self._voice_prov_label.setFixedWidth(96)
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
        self._voice_voice_label.setFixedWidth(96)
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
        self._kokoro_tag = QLabel(i18n.t("tts.kokoro_desc"))
        self._kokoro_tag.setObjectName("providerSub")
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

    # ── Section 3: Whisper providers (speech-to-text) ──────────────────────

    def _build_stt_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._stt_title = QLabel(i18n.t("settings.whisper_providers"))
        self._stt_title.setObjectName("sectionTitle")
        self._stt_sub = QLabel(i18n.t("settings.whisper_providers_sub"))
        self._stt_sub.setObjectName("sectionSub")
        self._stt_sub.setWordWrap(True)
        l.addWidget(self._stt_title)
        l.addWidget(self._stt_sub)

        # Active provider selector: faster-whisper (local) or Groq Whisper.
        prov_row = QHBoxLayout()
        self._stt_prov_label = QLabel(i18n.t("settings.active_provider"))
        self._stt_prov_label.setObjectName("fieldLabel")
        self._stt_prov_label.setFixedWidth(96)
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
        self._stt_model_label.setFixedWidth(96)
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
        self._stt_fallback_label.setFixedWidth(96)
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
        self._stt_fallback_model_label.setFixedWidth(96)
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
        self._liveapi_sub = QLabel(i18n.t("settings.liveapi_sub"))
        self._liveapi_sub.setObjectName("sectionSub")
        self._liveapi_sub.setWordWrap(True)
        l.addWidget(self._liveapi_title)
        l.addWidget(self._liveapi_sub)

        # Master toggle — the configuration body below stays hidden until ON.
        enable_row = QHBoxLayout()
        self._liveapi_enabled_label = QLabel(i18n.t("liveapi.enabled"))
        self._liveapi_enabled_label.setObjectName("providerName")
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
        self._liveapi_prov_label.setFixedWidth(96)
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
        self._liveapi_model_label.setFixedWidth(96)
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

    # ── Mobile connection (pairing token) ──────────────────────────────────

    def _build_mobile_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._mobile_title = QLabel(i18n.t("settings.mobile"))
        self._mobile_title.setObjectName("sectionTitle")
        self._mobile_sub = QLabel(i18n.t("settings.mobile_sub"))
        self._mobile_sub.setObjectName("sectionSub")
        self._mobile_sub.setWordWrap(True)
        l.addWidget(self._mobile_title)
        l.addWidget(self._mobile_sub)

        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        self._mobile_token_label = QLabel(i18n.t("settings.mobile_token"))
        self._mobile_token_label.setObjectName("fieldLabel")
        self._mobile_token_label.setFixedWidth(60)
        self._token_mask_label = QLabel(_mask_token(""))
        self._token_mask_label.setObjectName("tokenMask")
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
        self._ai_prov_label.setFixedWidth(96)
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
        olh.addWidget(ol_name)
        olh.addStretch()
        self._ollama_tag = QLabel(i18n.t("provider.no_key_required"))
        self._ollama_tag.setObjectName("providerSub")
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
        self._ai_title.setText(i18n.t("settings.ai_providers"))
        self._ai_sub.setText(i18n.t("settings.ai_providers_sub"))
        self._ai_prov_label.setText(i18n.t("settings.choose_provider"))

        self._voice_title.setText(i18n.t("settings.voice_providers"))
        self._voice_sub.setText(i18n.t("settings.voice_providers_sub"))
        self._voice_prov_label.setText(i18n.t("settings.active_provider"))
        self._voice_voice_label.setText(i18n.t("settings.voice"))
        self._kokoro_tag.setText(i18n.t("tts.kokoro_desc"))
        self._tts_provider_combo.setItemText(0, i18n.t("tts.local_option"))

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

        self._mobile_title.setText(i18n.t("settings.mobile"))
        self._mobile_sub.setText(i18n.t("settings.mobile_sub"))
        self._mobile_token_label.setText(i18n.t("settings.mobile_token"))
        self._mobile_show_btn.setText(i18n.t("settings.show"))
        self._mobile_regen_btn.setText(i18n.t("settings.regenerate"))
        self._mobile_warn.setText(i18n.t("settings.mobile_token_warning"))

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


# ── Bottom-nav button ────────────────────────────────────────────────────────

class _NavButton(QPushButton):
    """A bottom-nav button that paints a crisp vector icon.

    Qt's emoji rendering is inconsistent across platforms, so each nav entry
    draws its own glyph via ``QPainterPath``:

    * ``conv``     — a rounded speech bubble (Conversations)
    * ``models``   — a four-point sparkle/star (Models)
    * ``settings`` — a gear (Settings)

    The active tab gets a rounded violet pill behind the icon and the icon
    itself switches to the violet accent; hover shows a softer violet wash.
    """

    ICON_PX = 22

    def __init__(self, kind: str, tooltip: str):
        super().__init__()
        self._kind = kind
        self.setObjectName("navBtn")
        self.setCheckable(True)
        self.setFixedSize(52, 40)
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

        # Rounded pill background — solid violet wash when active, a softer
        # one on hover.
        if active:
            p.setPen(QPen(QColor(196, 181, 245, 180), 1))
            p.setBrush(QColor(196, 181, 245, 64))
            p.drawRoundedRect(rect, 12, 12)
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

    # Index of the chat page in the QStackedWidget.
    PAGE_CHAT = 0
    PAGE_CONVS = 1
    PAGE_MODELS = 2
    PAGE_SETTINGS = 3

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

        self._stack.addWidget(self._chat)
        self._stack.addWidget(self._convs)
        self._stack.addWidget(self._models)
        self._stack.addWidget(self._settings)
        outer.addWidget(self._stack, 1)

        # Bottom nav
        nav = QHBoxLayout()
        nav.setSpacing(12)
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        self._nav_buttons: dict[int, QPushButton] = {}
        for page_idx, kind, tooltip in [
            (self.PAGE_CONVS,   "conv",     "Conversations"),
            (self.PAGE_MODELS,  "models",   "Model"),
            (self.PAGE_SETTINGS,"settings", "Settings"),
        ]:
            btn = _NavButton(kind, tooltip)
            btn.clicked.connect(
                lambda _checked=False, idx=page_idx: self._on_nav_clicked(idx)
            )
            self._nav_group.addButton(btn, page_idx)
            self._nav_buttons[page_idx] = btn

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

    def append_message(self, role: str, text: str) -> None:
        self._chat.append_message(role, text)

    def refresh_conversations(self, active_id: str | None) -> None:
        self._convs.set_active(active_id)

    def set_current_model(self, model_id: str | None) -> None:
        self._models.set_current(model_id)

    def set_remote_models(self, remote: list) -> None:
        self._models.set_remote_models(remote)

    # ── Internal handlers ─────────────────────────────────────────────────

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
                color: #f5f5fa;
                font-family: -apple-system, "Segoe UI", "SF Pro Text",
                             "Helvetica Neue", sans-serif;
            }

            QPushButton#closeBtn {
                background: rgba(255, 255, 255, 0.06);
                color: rgba(245, 245, 250, 0.7);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 13px;
                font-size: 12px;
            }
            QPushButton#closeBtn:hover {
                background: rgba(255, 255, 255, 0.16);
                color: #ffffff;
            }
            QPushButton#minBtn {
                background: rgba(255, 255, 255, 0.06);
                color: rgba(245, 245, 250, 0.7);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 13px;
                font-size: 16px;
                font-weight: 600;
            }
            QPushButton#minBtn:hover {
                background: rgba(196, 181, 245, 0.22);
                color: #ffffff;
            }

            /* Panels ─────────────────────────────────────────────────── */
            QLabel#panelTitle {
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#panelSub {
                font-size: 11px;
                color: #8b7fb5;
            }
            QLabel#panelEmpty {
                color: #8b7fb5;
                font-size: 12px;
                padding: 30px 10px;
            }

            QScrollArea#chatScroll, QScrollArea#panelScroll,
            QWidget#chatHost, QWidget#panelHost {
                background: transparent;
                border: none;
            }
            /* The scroll-area viewport + its host pick up an opaque grey
               palette background by default — force every layer back to
               transparent so panels sit on the app's dark #0d0b12 glass. */
            QScrollArea#chatScroll > QWidget > QWidget,
            QScrollArea#panelScroll > QWidget > QWidget {
                background: transparent;
            }

            /* Chat ───────────────────────────────────────────────────── */
            QLabel#chatEmpty {
                color: #8b7fb5;
                font-size: 12px;
                padding: 40px 12px;
            }
            QFrame#msgBubble {
                border-radius: 16px;
                padding: 0px;
            }
            QFrame#msgBubble[role="user"] {
                background: rgba(140, 110, 220, 0.35);
                border: 1px solid rgba(196, 181, 245, 0.45);
            }
            QFrame#msgBubble[role="assistant"] {
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
            }
            QLabel#msgText {
                color: #f5f5fa;
                font-size: 12px;
                background: transparent;
            }

            QFrame#inputWrap {
                background: #1a1625;
                border: 1px solid rgba(196, 181, 245, 0.16);
                border-radius: 16px;
            }
            QFrame#inputWrap:hover {
                border: 1px solid rgba(196, 181, 245, 0.34);
            }
            QLineEdit#chatInput {
                background: transparent;
                border: none;
                color: #f5f5fa;
                font-size: 13px;
                padding: 4px 2px;
            }
            QPushButton#iconBtn {
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 17px;
                color: rgba(245, 245, 250, 0.8);
                font-size: 16px;
                font-weight: 600;
            }
            QPushButton#iconBtn:hover {
                background: rgba(196, 181, 245, 0.20);
                color: #ffffff;
            }
            QPushButton#sendBtn {
                background: rgba(196, 181, 245, 0.85);
                border: none;
                border-radius: 17px;
                color: #1a1422;
                font-size: 13px;
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
                border: 1px solid rgba(196, 181, 245, 0.08);
                border-radius: 12px;
            }
            QFrame#convRow:hover {
                border: 1px solid rgba(196, 181, 245, 0.35);
                background: #1a1625;
            }
            QFrame#convRow[active="true"] {
                background: rgba(196, 181, 245, 0.16);
                border: 1px solid rgba(196, 181, 245, 0.5);
            }
            QLabel#convTitle {
                font-size: 13px;
                font-weight: 600;
                padding-left: 2px;
            }
            QLabel#convSub {
                font-size: 11px;
                color: #8b7fb5;
            }
            QPushButton#convOpen {
                background: rgba(196, 181, 245, 0.20);
                border: 1px solid rgba(196, 181, 245, 0.40);
                color: #f5f0ff;
                border-radius: 8px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton#convOpen:hover {
                background: rgba(196, 181, 245, 0.35);
            }
            QPushButton#convDel {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.08);
                color: rgba(245, 245, 250, 0.6);
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
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 3px 10px;
                font-size: 12px;
                color: rgba(245, 245, 250, 0.85);
            }
            QPushButton#backBtn:hover {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #ffffff;
            }
            QFrame#provGridCard {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.10);
                border-radius: 12px;
            }
            QFrame#provGridCard:hover {
                border: 1px solid rgba(196, 181, 245, 0.45);
                background: #1a1625;
            }
            QLabel#provGridName {
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#provGridCount {
                font-size: 11px;
                color: rgba(245, 245, 250, 0.45);
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
                border: 1px solid rgba(196, 181, 245, 0.08);
                border-radius: 12px;
            }
            QFrame#modelRow:hover {
                border: 1px solid rgba(196, 181, 245, 0.35);
                background: #1a1625;
            }
            QFrame#modelRow[active="true"] {
                background: rgba(196, 181, 245, 0.16);
                border: 1px solid rgba(196, 181, 245, 0.5);
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
                font-size: 10px;
                color: #8b7fb5;
            }
            QPushButton#modelUse {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.40);
                color: #f5f0ff;
                border-radius: 8px;
                padding: 4px 12px;
                font-size: 11px;
            }
            QPushButton#modelUse:hover {
                background: rgba(196, 181, 245, 0.35);
            }
            QPushButton#modelUse:disabled {
                background: rgba(196, 181, 245, 0.22);
                border: 1px solid rgba(196, 181, 245, 0.5);
                color: rgba(245, 245, 250, 0.7);
            }

            /* Settings sections ─────────────────────────────────────── */
            QFrame#section {
                background: #0f0d16;
                border: 1px solid rgba(196, 181, 245, 0.08);
                border-radius: 12px;
            }
            QFrame#section:hover {
                border: 1px solid rgba(196, 181, 245, 0.32);
            }
            QLabel#sectionTitle {
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#sectionSub {
                font-size: 11px;
                color: #8b7fb5;
            }
            QLabel#fieldLabel {
                font-size: 11px;
                color: rgba(245, 245, 250, 0.55);
            }
            QLabel#startupLabel {
                font-size: 12px;
                color: rgba(245, 245, 250, 0.85);
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
                border: 1px solid rgba(196, 181, 245, 0.08);
                border-radius: 12px;
            }
            QFrame#providerCard:hover {
                border: 1px solid rgba(196, 181, 245, 0.35);
            }
            QFrame#providerCard:hover {
                border: 1px solid rgba(196, 181, 245, 0.40);
            }
            QLabel#providerName {
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#providerSub {
                font-size: 11px;
                color: #8b7fb5;
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
                color: #f5f0ff;
                letter-spacing: 1px;
            }
            QLabel#warnLabel {
                color: #e0a23c;
                font-size: 11px;
            }
            /* Live API (Bloc 4) — violet info box: STT + TTS bypassed notice */
            QLabel#liveApiNotice {
                background: rgba(196, 181, 245, 0.14);
                border: 1px solid rgba(196, 181, 245, 0.45);
                border-radius: 8px;
                padding: 8px 10px;
                color: #d9cffb;
                font-size: 11px;
            }
            QFrame#dialogCard {
                background: #14111c;
                border: 1px solid rgba(196, 181, 245, 0.40);
                border-radius: 14px;
            }
            QLabel#dialogTitle {
                font-size: 14px;
                font-weight: 600;
                color: #f5f5fa;
            }

            QPushButton#ghostBtn {
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 4px 12px;
                font-size: 11px;
                color: rgba(245, 245, 250, 0.85);
            }
            QPushButton#ghostBtn:hover {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #ffffff;
            }
            QPushButton#primaryBtn {
                background: rgba(196, 181, 245, 0.22);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #f5f0ff;
                border-radius: 8px;
                padding: 5px 14px;
                font-size: 12px;
                font-weight: 500;
            }
            QPushButton#primaryBtn:hover {
                background: rgba(196, 181, 245, 0.36);
            }
            QPushButton#linkBtn {
                background: transparent;
                border: none;
                color: rgba(245, 245, 250, 0.55);
                font-size: 11px;
                text-decoration: underline;
            }
            QPushButton#linkBtn:hover {
                color: #ffffff;
            }

            QLineEdit#keyInput {
                background: #14111c;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                padding: 6px 10px;
                color: #f5f5fa;
                font-size: 12px;
            }
            QLineEdit#keyInput:focus {
                border: 1px solid rgba(196, 181, 245, 0.55);
            }
            QPushButton#saveBtn {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
                padding: 5px 18px;
                color: #f5f5fa;
                font-size: 12px;
            }
            QPushButton#saveBtn:hover {
                background: rgba(196, 181, 245, 0.30);
                border: 1px solid rgba(196, 181, 245, 0.55);
            }
            QPushButton#saveBtn[saved="true"] {
                background: rgba(196, 181, 245, 0.35);
                border: 1px solid rgba(196, 181, 245, 0.85);
                color: #ffffff;
            }

            /* Voice combos ──────────────────────────────────────────── */
            QComboBox#voiceCombo {
                background: #14111c;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 5px 10px;
                color: #f5f5fa;
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
                border-top: 6px solid rgba(245, 245, 250, 0.5);
                margin-right: 6px;
            }
            QComboBox#voiceCombo QAbstractItemView {
                background: rgb(28, 24, 36);
                color: #f5f5fa;
                border: 1px solid rgba(196, 181, 245, 0.30);
                border-radius: 6px;
                selection-background-color: rgba(196, 181, 245, 0.30);
                font-size: 12px;
            }

            /* Smart-toggle ──────────────────────────────────────────── */
            QCheckBox#smartToggle::indicator {
                width: 36px;
                height: 20px;
                border-radius: 10px;
                background: rgba(255, 255, 255, 0.15);
                border: 1px solid rgba(255, 255, 255, 0.15);
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
                background: rgba(255, 255, 255, 0.18);
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
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
