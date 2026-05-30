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
import threading
from datetime import datetime

from PyQt6.QtCore import (
    Qt, QEvent, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QPoint,
    QRectF, QPointF,
)
from PyQt6.QtGui import (
    QColor, QPainter, QFont, QPainterPath, QGuiApplication, QPen,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QStackedWidget, QFrame, QGraphicsDropShadowEffect,
    QSizePolicy, QFileDialog, QCheckBox, QComboBox, QDialog, QMessageBox,
    QButtonGroup,
)

from osia import conversations
from osia.models import (
    PROVIDERS, API_PROVIDERS, HARDCODED_MODELS, get_all_models,
)
from osia.config import (
    load_api_keys, save_api_keys, load_settings, set_setting, set_api_key,
)
from osia.utils.systemd_manager import (
    is_service_enabled, enable_service, disable_service, get_service_status,
)
from osia.auth import oauth as oauth_module


# ── Visual constants ────────────────────────────────────────────────────────

POPUP_W = 480
POPUP_H = 680
RADIUS = 20
# Dark, semi-transparent glassmorphism fill so the popup reads as a floating
# glass panel rather than an opaque window. Lower alpha → more transparent;
# a second, slightly lighter layer painted on top fakes a soft blur/frost.
BG_COLOR = QColor(13, 11, 18, 180)
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


# ── Helpers ─────────────────────────────────────────────────────────────────

def _format_time(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return ts


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
        self._empty = QLabel("Say “OSIA” or type below to start.")
        self._empty.setObjectName("chatEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        while self._messages_layout.count() > 1:
            item = self._messages_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._empty.show()
        self._messages_layout.insertWidget(0, self._empty)

    def set_messages(self, messages: list[dict]) -> None:
        self.clear_messages()
        # Drop the empty placeholder if there's anything to show.
        renderable = [
            m for m in messages
            if m.get("role") in ("user", "assistant")
            and not str(m.get("content", "")).lstrip().startswith("<tool_result>")
        ]
        if not renderable:
            return
        self._empty.hide()
        for m in renderable:
            self._append_bubble(m["role"], m.get("content", ""))
        QTimer.singleShot(0, self._scroll_to_bottom)

    def append_message(self, role: str, text: str) -> None:
        if not text:
            return
        if str(text).lstrip().startswith("<tool_result>"):
            return
        self._empty.hide()
        self._append_bubble(role, text)
        QTimer.singleShot(0, self._scroll_to_bottom)

    # ── Internals ─────────────────────────────────────────────────────────

    def _append_bubble(self, role: str, text: str) -> None:
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
        title = QLabel(conv.get("title") or "New conversation")
        title.setObjectName("convTitle")
        title.setWordWrap(False)
        title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
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

        self._host = QWidget()
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
            placeholder = QLabel("No conversations yet.")
            placeholder.setObjectName("panelEmpty")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._host_layout.insertWidget(0, placeholder)
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


class _ModelPanel(QWidget):
    """List of all available models, click to select."""

    model_changed = pyqtSignal(str)

    PROVIDER_ORDER = ("anthropic", "openai", "deepseek", "google", "xai", "ollama")

    def __init__(self):
        super().__init__()
        self._current_model: str | None = None
        self._remote: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Models")
        title.setObjectName("panelTitle")
        header.addWidget(title)
        header.addStretch()
        outer.addLayout(header)

        sub = QLabel("Pick the model OSIA will use for new messages.")
        sub.setObjectName("panelSub")
        outer.addWidget(sub)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._host = QWidget()
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

    def refresh(self) -> None:
        while self._host_layout.count() > 1:
            item = self._host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        models = get_all_models(remote_models=self._remote)
        models.sort(key=lambda m: (
            self.PROVIDER_ORDER.index(m["provider"])
            if m["provider"] in self.PROVIDER_ORDER
            else len(self.PROVIDER_ORDER),
            m.get("label", ""),
        ))
        idx = 0
        last_provider = None
        for m in models:
            if m["provider"] != last_provider:
                header = QLabel(
                    PROVIDERS.get(m["provider"], {}).get("name")
                    or m["provider"].title()
                )
                header.setObjectName("modelGroup")
                self._host_layout.insertWidget(idx, header)
                idx += 1
                last_provider = m["provider"]
            row = _ModelRow(m, is_active=(m["id"] == self._current_model))
            row.selected.connect(self.model_changed.emit)
            self._host_layout.insertWidget(idx, row)
            idx += 1


# ── Settings panel ──────────────────────────────────────────────────────────

class _ProviderCard(QFrame):
    """One AI provider in the settings page.

    Shows the provider name, a 'Choose / Configure' toggle that reveals an
    API-key input + Save button. The save button turns green once the key
    is persisted.
    """

    saved = pyqtSignal(str)            # provider
    oauth_connect = pyqtSignal(str)    # provider
    oauth_disconnect = pyqtSignal(str) # provider

    def __init__(self, provider: str):
        super().__init__()
        self.setObjectName("providerCard")
        self._provider = provider

        cfg = PROVIDERS[provider]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        name = QLabel(cfg["name"])
        name.setObjectName("providerName")
        self._status_dot = QLabel()
        self._status_dot.setObjectName("statusDot")
        self._status_dot.setFixedSize(8, 8)
        self._status_dot.setProperty("state", "stopped")

        self._toggle_btn = QPushButton("Configure")
        self._toggle_btn.setObjectName("ghostBtn")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_body)

        head_row.addWidget(self._status_dot)
        head_row.addWidget(name)
        head_row.addStretch()
        head_row.addWidget(self._toggle_btn)
        layout.addLayout(head_row)

        # Body — hidden by default
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 4, 0, 0)
        body_layout.setSpacing(8)

        models = [m for m in HARDCODED_MODELS if m["provider"] == provider]
        if models:
            names = QLabel(", ".join(m["label"] for m in models))
            names.setObjectName("providerSub")
            names.setWordWrap(True)
            body_layout.addWidget(names)

        # OAuth row (only for providers that support it)
        if provider in oauth_module.PROVIDERS:
            oauth_row = QHBoxLayout()
            self._oauth_btn = QPushButton("Se connecter avec Claude")
            self._oauth_btn.setObjectName("primaryBtn")
            self._oauth_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._oauth_btn.clicked.connect(
                lambda: self.oauth_connect.emit(self._provider)
            )
            self._oauth_label = QLabel("Connecté ✓")
            self._oauth_label.setObjectName("oauthOk")
            self._oauth_disc = QPushButton("Déconnecter")
            self._oauth_disc.setObjectName("linkBtn")
            self._oauth_disc.setCursor(Qt.CursorShape.PointingHandCursor)
            self._oauth_disc.clicked.connect(
                lambda: self.oauth_disconnect.emit(self._provider)
            )
            oauth_row.addWidget(self._oauth_btn)
            oauth_row.addWidget(self._oauth_label)
            oauth_row.addStretch()
            oauth_row.addWidget(self._oauth_disc)
            body_layout.addLayout(oauth_row)
        else:
            self._oauth_btn = None
            self._oauth_label = None
            self._oauth_disc = None

        # API key row
        key_row = QHBoxLayout()
        self._key_input = QLineEdit()
        self._key_input.setObjectName("keyInput")
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText(f"{cfg['name']} API key")
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("saveBtn")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)
        key_row.addWidget(self._key_input, 1)
        key_row.addWidget(self._save_btn)
        body_layout.addLayout(key_row)

        self._body.hide()
        layout.addWidget(self._body)

    def set_key(self, key: str) -> None:
        self._key_input.setText(key or "")
        self._update_status_dot()

    def current_key(self) -> str:
        return self._key_input.text().strip()

    def refresh_oauth(self, connected: bool) -> None:
        if self._oauth_btn is None:
            return
        self._oauth_btn.setVisible(not connected)
        self._oauth_label.setVisible(connected)
        self._oauth_disc.setVisible(connected)
        self._update_status_dot(force_connected=connected)

    def _update_status_dot(self, force_connected: bool | None = None) -> None:
        connected = (
            force_connected
            if force_connected is not None
            else bool(self.current_key())
        )
        if (self._oauth_btn is not None
                and force_connected is None
                and oauth_module.is_connected(self._provider)):
            connected = True
        self._status_dot.setProperty(
            "state", "running" if connected else "stopped"
        )
        self._status_dot.style().unpolish(self._status_dot)
        self._status_dot.style().polish(self._status_dot)

    def _toggle_body(self) -> None:
        if self._body.isVisible():
            self._body.hide()
            self._toggle_btn.setText("Configure")
        else:
            self._body.show()
            self._toggle_btn.setText("Hide")

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
        self._save_btn.setText("Saved ✓")
        self._save_btn.setProperty("saved", True)
        self._save_btn.style().unpolish(self._save_btn)
        self._save_btn.style().polish(self._save_btn)

        def restore():
            self._save_btn.setText("Save")
            self._save_btn.setProperty("saved", False)
            self._save_btn.style().unpolish(self._save_btn)
            self._save_btn.style().polish(self._save_btn)

        QTimer.singleShot(1800, restore)


class _SettingsPanel(QWidget):
    """Settings page — redesigned with the 'Choose provider' card style.

    Re-uses the original feature set: smart routing toggle, voice provider
    selection, startup systemd services, and per-provider API key + OAuth.
    """

    smart_routing_toggled = pyqtSignal(bool)
    api_keys_saved = pyqtSignal(dict)

    _oauth_finished = pyqtSignal(str, bool, str)  # provider, ok, message

    _TTS_PROVIDER_LABELS = [
        ("local",  "Local (Kokoro)"),
        ("openai", "OpenAI"),
        ("google", "Google Cloud"),
    ]

    def __init__(self):
        super().__init__()

        self._provider_cards: dict[str, _ProviderCard] = {}
        self._startup_refresh_guard = False
        self._startup_rows: list[tuple[str, QLabel, QCheckBox]] = []
        self._drag_pos: QPoint | None = None
        self._oauth_dialog: QDialog | None = None

        self._oauth_finished.connect(self._on_oauth_finished)

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

        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(14)

        v.addWidget(self._build_smart_section())
        v.addWidget(self._build_startup_section())
        v.addWidget(self._build_voice_section())
        v.addWidget(self._build_ai_providers_section())
        v.addStretch(1)

        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        self._apply_style()
        self._load_state()

        # Refresh startup status while visible.
        self._startup_timer = QTimer(self)
        self._startup_timer.setInterval(5000)
        self._startup_timer.timeout.connect(self._refresh_startup_status)

    # ── Section builders ──────────────────────────────────────────────────

    def _build_smart_section(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(4)

        row = QHBoxLayout()
        label = QLabel("Smart model selection")
        label.setObjectName("sectionTitle")
        self._smart_toggle = QCheckBox()
        self._smart_toggle.setObjectName("smartToggle")
        self._smart_toggle.stateChanged.connect(self._on_smart_changed)
        row.addWidget(label)
        row.addStretch()
        row.addWidget(self._smart_toggle)
        l.addLayout(row)

        sub = QLabel("Auto-pick a model per prompt.")
        sub.setObjectName("sectionSub")
        l.addWidget(sub)
        return wrap

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

    def _build_voice_section(self) -> QFrame:
        from osia.voice.tts import (
            OPENAI_VOICES, GOOGLE_FR_VOICES, GOOGLE_EN_VOICES,
            PROVIDER_DEFAULT_VOICE,
        )
        self._openai_voices = list(OPENAI_VOICES)
        self._google_voices = (
            [("French — " + v, v) for v in GOOGLE_FR_VOICES]
            + [("English — " + v, v) for v in GOOGLE_EN_VOICES]
        )
        self._provider_default_voice = dict(PROVIDER_DEFAULT_VOICE)

        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        title = QLabel("Voice")
        title.setObjectName("sectionTitle")
        sub = QLabel("Choose how OSIA speaks. Changes apply immediately.")
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)
        l.addWidget(title)
        l.addWidget(sub)

        prov_row = QHBoxLayout()
        prov_label = QLabel("Provider")
        prov_label.setObjectName("fieldLabel")
        prov_label.setFixedWidth(60)
        self._tts_provider_combo = QComboBox()
        self._tts_provider_combo.setObjectName("voiceCombo")
        for key, label in self._TTS_PROVIDER_LABELS:
            self._tts_provider_combo.addItem(label, key)
        self._tts_provider_combo.currentIndexChanged.connect(
            self._on_tts_provider_changed
        )
        prov_row.addWidget(prov_label)
        prov_row.addWidget(self._tts_provider_combo, 1)
        l.addLayout(prov_row)

        self._tts_voice_row = QWidget()
        voice_row = QHBoxLayout(self._tts_voice_row)
        voice_row.setContentsMargins(0, 0, 0, 0)
        voice_label = QLabel("Voice")
        voice_label.setObjectName("fieldLabel")
        voice_label.setFixedWidth(60)
        self._tts_voice_combo = QComboBox()
        self._tts_voice_combo.setObjectName("voiceCombo")
        self._tts_voice_combo.currentIndexChanged.connect(self._on_tts_voice_changed)
        voice_row.addWidget(voice_label)
        voice_row.addWidget(self._tts_voice_combo, 1)
        l.addWidget(self._tts_voice_row)

        return wrap

    def _build_ai_providers_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        title = QLabel("AI providers")
        title.setObjectName("sectionTitle")
        sub = QLabel("Configure an API key for each provider you want to use.")
        sub.setObjectName("sectionSub")
        sub.setWordWrap(True)
        l.addWidget(title)
        l.addWidget(sub)

        for provider in API_PROVIDERS:
            card = _ProviderCard(provider)
            card.saved.connect(self._on_provider_saved)
            card.oauth_connect.connect(self._on_oauth_connect)
            card.oauth_disconnect.connect(self._on_oauth_disconnect)
            l.addWidget(card)
            self._provider_cards[provider] = card

        # Ollama is keyless — show it as info only.
        ollama_card = QFrame()
        ollama_card.setObjectName("providerCard")
        ol = QVBoxLayout(ollama_card)
        ol.setContentsMargins(14, 12, 14, 12)
        olh = QHBoxLayout()
        olh.addWidget(QLabel("Local (Ollama)"))
        olh.addStretch()
        tag = QLabel("No key required")
        tag.setObjectName("providerSub")
        olh.addWidget(tag)
        ol.addLayout(olh)
        l.addWidget(ollama_card)

        return wrap

    # ── State load / save ─────────────────────────────────────────────────

    def _load_state(self) -> None:
        keys = load_api_keys()
        for provider, card in self._provider_cards.items():
            card.set_key(keys.get(provider, ""))
            if provider in oauth_module.PROVIDERS:
                card.refresh_oauth(oauth_module.is_connected(provider))

        settings = load_settings()
        self._smart_toggle.blockSignals(True)
        self._smart_toggle.setChecked(bool(settings.get("smart_routing", False)))
        self._smart_toggle.blockSignals(False)

        provider = settings.get("tts_provider", "local") or "local"
        voice = settings.get("tts_voice")
        self._tts_provider_combo.blockSignals(True)
        for i in range(self._tts_provider_combo.count()):
            if self._tts_provider_combo.itemData(i) == provider:
                self._tts_provider_combo.setCurrentIndex(i)
                break
        self._tts_provider_combo.blockSignals(False)
        self._populate_voice_combo(provider, voice)

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

    # ── Handlers ──────────────────────────────────────────────────────────

    def _on_smart_changed(self, _state) -> None:
        checked = self._smart_toggle.isChecked()
        set_setting("smart_routing", checked)
        self.smart_routing_toggled.emit(checked)

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

    def _on_provider_saved(self, _provider: str) -> None:
        self.api_keys_saved.emit(load_api_keys())

    def _on_oauth_connect(self, provider: str) -> None:
        try:
            session = oauth_module.start_oauth_flow(provider)
        except oauth_module.OAuthError as e:
            QMessageBox.warning(self, "Connexion impossible", str(e))
            return
        self._prompt_oauth_code(provider, session)

    def _prompt_oauth_code(self, provider: str, session) -> None:
        """Show a non-blocking dialog asking for the pasted auth code.

        Uses ``QDialog.show()`` (non-blocking) instead of ``exec()`` so the
        user can keep interacting with the browser and other windows while
        the code is being entered.
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("Connexion Claude")
        dialog.setObjectName("oauthDialog")
        dialog.setModal(False)

        lay = QVBoxLayout(dialog)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        label = QLabel(
            "Authentifiez-vous dans le navigateur. Collez le code ici :"
        )
        label.setWordWrap(True)

        code_input = QLineEdit()
        code_input.setObjectName("keyInput")
        code_input.setPlaceholderText("Code d'autorisation")

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Annuler")
        cancel_btn.setObjectName("ghostBtn")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn = QPushButton("Valider")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)

        lay.addWidget(label)
        lay.addWidget(code_input)
        lay.addLayout(btn_row)

        cancel_btn.clicked.connect(dialog.reject)
        ok_btn.clicked.connect(dialog.accept)
        code_input.returnPressed.connect(dialog.accept)

        def on_finished(result: int) -> None:
            self._oauth_dialog = None
            if result != QDialog.DialogCode.Accepted:
                return
            code = code_input.text().strip()
            if code:
                self._complete_oauth_async(provider, session, code)

        dialog.finished.connect(on_finished)
        # Keep a reference so the non-blocking dialog isn't garbage-collected.
        self._oauth_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _complete_oauth_async(self, provider: str, session, code: str) -> None:
        def worker():
            try:
                oauth_module.complete_oauth_flow(session, code)
                self._oauth_finished.emit(provider, True, "")
            except oauth_module.OAuthError as e:
                self._oauth_finished.emit(provider, False, str(e))
            except Exception as e:
                self._oauth_finished.emit(provider, False, f"Erreur: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_oauth_disconnect(self, provider: str) -> None:
        oauth_module.revoke_token(provider)
        card = self._provider_cards.get(provider)
        if card is not None:
            card.refresh_oauth(False)

    def _on_oauth_finished(self, provider: str, ok: bool, message: str) -> None:
        card = self._provider_cards.get(provider)
        if card is not None:
            card.refresh_oauth(oauth_module.is_connected(provider))
        if not ok:
            QMessageBox.warning(self, "Connexion échouée",
                                message or "Échec de l'OAuth")

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
    smart_routing_toggled = pyqtSignal(bool)
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

        # Soft shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(60)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 220))
        self.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(8)

        # Header (title + close)
        header = QHBoxLayout()
        title = QLabel("OSIA")
        title.setObjectName("popupTitle")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(26, 26)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(close_btn)
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
        self._settings.smart_routing_toggled.connect(self.smart_routing_toggled.emit)
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

    def anchor_below_island(self, island_geom_bottom_y: int = 60) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
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

            QLabel#popupTitle {
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 3px;
                color: rgba(245, 245, 250, 0.85);
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
                color: rgba(245, 245, 250, 0.55);
            }
            QLabel#panelEmpty {
                color: rgba(245, 245, 250, 0.4);
                font-size: 12px;
                padding: 30px 10px;
            }

            QScrollArea#chatScroll, QScrollArea#panelScroll,
            QWidget#chatHost {
                background: transparent;
                border: none;
            }

            /* Chat ───────────────────────────────────────────────────── */
            QLabel#chatEmpty {
                color: rgba(245, 245, 250, 0.4);
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
                background: #1a1625;
                border: 1px solid rgba(196, 181, 245, 0.14);
                border-radius: 14px;
            }
            QFrame#convRow:hover {
                border: 1px solid rgba(196, 181, 245, 0.45);
                background: #1f1a2c;
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
                color: rgba(245, 245, 250, 0.45);
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
                background: #1a1625;
                border: 1px solid rgba(196, 181, 245, 0.14);
                border-radius: 14px;
            }
            QFrame#modelRow:hover {
                border: 1px solid rgba(196, 181, 245, 0.45);
                background: #1f1a2c;
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
                color: rgba(245, 245, 250, 0.4);
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
                background: rgba(105, 200, 140, 0.22);
                border: 1px solid rgba(105, 200, 140, 0.5);
                color: rgba(245, 245, 250, 0.7);
            }

            /* Settings sections ─────────────────────────────────────── */
            QFrame#section {
                background: #1a1625;
                border: 1px solid rgba(196, 181, 245, 0.14);
                border-radius: 16px;
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
                color: rgba(245, 245, 250, 0.5);
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
                background: rgb(105, 200, 140);
            }
            QLabel#statusDot[state="stopped"] {
                background: rgba(180, 180, 180, 0.4);
            }
            QLabel#statusDot[state="error"] {
                background: rgb(220, 160, 60);
            }

            /* Provider card (Choose / Configure) ────────────────────── */
            QFrame#providerCard {
                background: #1a1625;
                border: 1px solid rgba(196, 181, 245, 0.14);
                border-radius: 14px;
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
                color: rgba(245, 245, 250, 0.5);
            }
            QLabel#oauthOk {
                color: rgb(120, 220, 140);
                font-size: 12px;
                font-weight: 600;
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
                background: rgba(105, 200, 140, 0.35);
                border: 1px solid rgba(105, 200, 140, 0.85);
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
