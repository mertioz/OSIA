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

import json
import math
import re
import shlex
import sys
import threading
import webbrowser
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import qrcode

from PyQt6.QtCore import (
    Qt, QEvent, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QPoint,
    QRect, QRectF, QPointF, QSize, QFileSystemWatcher, QVariantAnimation,
)
from PyQt6.QtGui import (
    QColor, QPainter, QFont, QPainterPath, QGuiApplication, QPen, QPixmap, QIcon,
    QTextOption, QImage,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QStackedWidget, QListWidget, QAbstractItemView, QFrame,
    QSizePolicy, QFileDialog, QComboBox,
    QButtonGroup, QGridLayout, QDialog, QTextEdit, QInputDialog, QLayout, QMenu,
    QToolTip, QMessageBox, QTextBrowser,
)

from osia import conversations
from osia import personality
from osia import legal
from osia.agent import strip_tool_artifacts
from osia.memory import MemoryStore
from osia.models import (
    PROVIDERS, get_all_models_cached as get_all_models, provider_for_model_cached,
    refresh_ollama_models_async, model_has_vision, vision_capable_models,
)
from osia.config import (
    load_api_keys, save_api_keys, load_settings, set_setting, set_api_key,
    get_api_key, load_mobile_token, regenerate_mobile_token,
    get_setting, get_mcp_token, set_mcp_token, delete_mcp_token,
    delete_mcp_oauth, get_mcp_oauth, set_mcp_oauth,
    get_oauth_client, set_oauth_client,
    get_reasoning_settings, set_reasoning_settings,
)
from osia.reasoning import get_reasoning_options
from osia.mobile_model_catalogue import (
    build_model_inventory,
    configured_api_providers,
    resolve_autonomous_selection,
)
from osia.utils.systemd_manager import (
    is_service_enabled, enable_service, disable_service, get_service_status,
    restart_service, stop_service,
)
from osia.utils.assets import LOGO_PATH, autostart_enabled, set_autostart
from osia import i18n
from osia import timefmt
from osia.autonomous import diagnostics as autonomous_diagnostics
from osia.perf import perf_timer
from osia.gui.motion import (
    MOTION, MotionController, reduced_motion_requested,
)
from osia.gui.switch import OsiaSwitch
from osia.gui.streaming import DesktopTextStreamController
from osia.gui.theme import PALETTES, get_theme_manager, themed_stylesheet
from osia.gui.update_ui import UpdateBanner, UpdateController, UpdateSettingsSection
from osia.desktop_attachments import (
    DesktopAttachmentError,
    DraftAttachment,
    readable_file_size,
    stage_attachment,
)
from osia.gui.slash_palette import SlashCommandPalette
from osia.slash_commands import (
    CapabilityRequirements,
    ConnectorHint,
    ConversationRef,
    EMPTY_DIRECTIVES,
    ModelOverride,
    SlashDirectiveError,
    SlashTurnDirectivesV1,
    consume_palette_prefix,
    escaped_literal_text,
)

# sip lets us detect widgets whose C++ side has already been destroyed
# (accessing them raises RuntimeError otherwise). It ships with PyQt6 but
# guard the import so a missing module never breaks the GUI.
try:
    from PyQt6 import sip
except ImportError:
    sip = None


# ── Visual constants ────────────────────────────────────────────────────────

# WOR-48: the popup was intentionally made smaller / more minimalist here
# (520×640 → 440×560). This deliberately overrides the CLAUDE.md POPUP_W=520
# protection — do not "restore" it to 520 without a new explicit request.
POPUP_W = 440
POPUP_H = 560
# WOR-48 P3 — the window width is dynamic per page: COMPACT (POPUP_W) for
# chat/conversations/models/OSIA, WIDE for the Settings master-detail layout
# (110px category nav + ~415px detail panel; the settings form fields carry no
# hard minimum width, so they adapt to this pane without clipping). Height never
# changes. WOR-48 audit #2: the width now SNAPS to its target in
# MainPopup._animate_width (no tween — the old animation slid the settings nav
# under the cursor and swallowed the first category click). The ~415px figure is
# narrower than the pre-audit ~470px because the card is now inset by
# SHADOW_MARGIN for the hand-painted drop shadow (see below).
POPUP_W_WIDE = 640
RADIUS = 16     # WOR-48 P3: 20 → 16, lightly-rounded rectangle (not pill-like)
COMPOSER_BUTTON_SIZE = 34
COMPOSER_BUTTON_RADIUS = 8
COMPOSER_ICON_SIZE = 14
# WOR-48 audit #2 — the non-Windows drop shadow is painted BY HAND in
# MainPopup.paintEvent
# (no QGraphicsDropShadowEffect, which makes Qt render the widget off-screen and
# thereby defeats the per-frame transparent clear, leaving ghost rectangles on
# the translucent moving/resizing window). SHADOW_MARGIN is the transparent
# canvas band reserved around the card for that shadow: the card is inset by it,
# and the window's outer layout margin = SHADOW_MARGIN + the card's own 11px
# internal padding (so content keeps exactly the breathing room it had before).
SHADOW_MARGIN = 27
SHADOW_LAYERS = 9     # concentric rounded-rect outlines faking a blurred shadow
SHADOW_STEP = 2       # px each successive ring expands outward from the card
SHADOW_DY = 8         # downward shadow offset (light reads as coming from above)
SHADOW_ALPHA = 45     # alpha of the innermost (darkest) ring; fades to 0 outward
# WOR-237: Windows must not paint the broad external halo.  The inset, window
# geometry, translucent clear and rounded glass card remain unchanged.
_PAINT_EXTERNAL_WINDOW_SHADOW = sys.platform != "win32"
# Dark, semi-transparent glassmorphism fill so the popup reads as a floating
# glass panel rather than an opaque window. Lower alpha → more transparent;
# a second, slightly lighter layer painted on top fakes a soft blur/frost.
# WOR-48: alpha lowered 220 → 160 → 125 for a clearly translucent "glass"
# panel where the desktop shows through (Arch-Linux translucent-terminal look).
# Inner surfaces (assistant bubble, input frame) keep a higher alpha so text
# stays legible while this outer card is strongly translucent.

STARTUP_SERVICES = ("osia-server.service",)

# Official API-key pages opened by the "Get API key" → Browse button on each
# provider's configuration card. Ollama is local and has no key page.
PROVIDER_KEY_URLS = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai":    "https://platform.openai.com/api-keys",
    "google":    "https://aistudio.google.com/apikey",
    "deepseek":  "https://platform.deepseek.com/api_keys",
    "xai":       "https://console.x.ai",
    "groq":      "https://console.groq.com/keys",
    "mistral":   "https://console.mistral.ai/api-keys",
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
    "mistral":   "#FA520F",   # Mistral orange
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


def _pairing_qr_pixmap(token: str, target_size: int = 252) -> QPixmap:
    """Render *token* as a high-contrast QR pixmap without writing it to disk."""
    if not token:
        return QPixmap()
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=4,
    )
    qr.add_data(token, optimize=0)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    module_size = max(1, target_size // len(matrix))
    image_size = len(matrix) * module_size
    image = QImage(image_size, image_size, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for row, modules in enumerate(matrix):
        for column, filled in enumerate(modules):
            if filled:
                painter.drawRect(
                    column * module_size,
                    row * module_size,
                    module_size,
                    module_size,
                )
    painter.end()
    return QPixmap.fromImage(image)


def _cap_combo_width(combo: QComboBox) -> None:
    """Stop a combo's minimum width from tracking its longest item text.

    WOR-48 P3 — a QComboBox sizes its minimum to the longest item by default,
    so one long "provider:model" id inflates the whole settings page's minimum
    width past the master-detail viewport and clips it. With the policy below
    the closed combo can compress (its text clips at the edge if truly
    squeezed) while the dropdown list still shows every item full-width.
    """
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    combo.setMinimumContentsLength(12)


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
    from osia.runtime import bundled_model_path

    hub = Path.home() / ".cache" / "huggingface" / "hub"
    found: set[str] = set()
    for size in ("tiny", "base", "small", "medium", "large-v3"):
        if bundled_model_path(f"faster-whisper-{size}") is not None:
            found.add(size)
    try:
        for entry in hub.iterdir():
            if entry.is_dir() and entry.name.startswith(_FASTER_WHISPER_REPO_PREFIX):
                found.add(entry.name[len(_FASTER_WHISPER_REPO_PREFIX):])
    except OSError:
        pass
    return found


# ── Message bubble ──────────────────────────────────────────────────────────

_BUBBLE_MAX_WIDTH = 360


class _MessageText(QTextBrowser):
    """Selectable message text with native anywhere fallback wrapping."""

    metrics_changed = pyqtSignal()

    def __init__(self, text: str):
        super().__init__()
        self.setObjectName("msgText")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setReadOnly(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(0)
        option = self.document().defaultTextOption()
        option.setWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
        )
        self.document().setDefaultTextOption(option)
        self.setPlainText(text)

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt method name)
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.DevicePixelRatioChange,
        ):
            self.metrics_changed.emit()

    def fit_to_width(self, maximum_width: int) -> None:
        """Constrain the document first, then size the viewport to its height."""

        document = self.document()
        document.setTextWidth(-1)
        natural_width = max(1, math.ceil(document.idealWidth()))
        text_width = max(1, min(int(maximum_width), natural_width))
        document.setTextWidth(text_width)
        text_height = max(
            1,
            math.ceil(document.documentLayout().documentSize().height()),
        )
        self.setFixedSize(text_width, text_height)


class _Bubble(QFrame):
    """One chat message bubble — user (right, violet) or assistant (left)."""

    def __init__(self, role: str, text: str):
        super().__init__()
        self.setObjectName("msgBubble")
        self.setProperty("role", role)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        self._source_text = text or ""
        self._available_width = _BUBBLE_MAX_WIDTH
        label = _MessageText(self._source_text)
        self._label = label
        label.metrics_changed.connect(self._layout_text)
        layout.addWidget(label)

        self.setMaximumWidth(_BUBBLE_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self._layout_text()

    def set_available_width(self, width: int) -> None:
        width = max(1, min(_BUBBLE_MAX_WIDTH, int(width)))
        if width == self._available_width:
            return
        self._available_width = width
        self.setMaximumWidth(width)
        self._layout_text()

    def _layout_text(self) -> None:
        margins = self.layout().contentsMargins()
        text_width = max(
            1,
            self._available_width - margins.left() - margins.right(),
        )
        self._label.fit_to_width(text_width)
        self.layout().invalidate()
        self.updateGeometry()
        row = self.parentWidget()
        if row is None or row.layout() is None:
            return
        row.layout().invalidate()
        row.updateGeometry()
        host = row.parentWidget()
        if host is not None and host.layout() is not None:
            host.layout().invalidate()
            host.layout().activate()
            row.layout().activate()
            host.updateGeometry()

    def append_text(self, more: str) -> None:
        self.set_text(self._source_text + more)

    def set_text(self, text: str) -> None:
        self._source_text = text
        self._label.setPlainText(text)
        self._layout_text()

    def text(self) -> str:
        return self._source_text

# ── Chat page ───────────────────────────────────────────────────────────────

class _ComposerButton(QPushButton):
    """Square composer control with a geometry-centred painted icon."""

    def __init__(self, kind: str):
        super().__init__()
        if kind not in ("attach", "send"):
            raise ValueError(f"Unsupported composer button kind: {kind}")
        self._kind = kind
        self.setFixedSize(COMPOSER_BUTTON_SIZE, COMPOSER_BUTTON_SIZE)

    def _icon_bounds(self) -> QRectF:
        side = float(COMPOSER_ICON_SIZE)
        return QRectF(
            (self.width() - side) / 2.0,
            (self.height() - side) / 2.0,
            side,
            side,
        )

    def _icon_path(self) -> QPainterPath:
        bounds = self._icon_bounds()
        center = bounds.center()
        path = QPainterPath()
        if self._kind == "attach":
            path.moveTo(bounds.left() + 2.0, center.y())
            path.lineTo(bounds.right() - 2.0, center.y())
            path.moveTo(center.x(), bounds.top() + 2.0)
            path.lineTo(center.x(), bounds.bottom() - 2.0)
        else:
            path.moveTo(bounds.left() + 2.0, center.y())
            path.lineTo(bounds.right() - 2.0, center.y())
            path.moveTo(bounds.right() - 6.0, bounds.top() + 3.0)
            path.lineTo(bounds.right() - 2.0, center.y())
            path.lineTo(bounds.right() - 6.0, bounds.bottom() - 3.0)
        return path

    def _icon_color(self) -> QColor:
        palette = get_theme_manager().palette
        if not self.isEnabled():
            return palette.color("disabled")
        if self._kind == "send":
            return palette.color("background")
        return palette.color("text", 215)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._icon_color(), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._icon_path())
        painter.end()


class _ComposerInput(QTextEdit):
    """Compact multiline editor: Enter submits, Shift+Enter inserts a newline."""

    send_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setFixedHeight(42)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.palette_key_handler = None

    def text(self) -> str:
        """Compatibility accessor for the former QLineEdit composer."""

        return self.toPlainText()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt method name)
        if callable(self.palette_key_handler) and self.palette_key_handler(event):
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self.send_requested.emit()
                event.accept()
                return
        super().keyPressEvent(event)


class _AttachmentChip(QFrame):
    """Path-free, removable presentation for one pending draft item."""

    remove_requested = pyqtSignal(str)

    def __init__(self, attachment: DraftAttachment) -> None:
        super().__init__()
        self._attachment = attachment
        self._display_name = attachment.display_name
        self.setObjectName("attachmentChip")
        self.setProperty("validation", attachment.validation_state)
        self.setAccessibleName(
            f"{attachment.display_name}, {readable_file_size(attachment.size_bytes)}"
        )
        self.setFixedHeight(34)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 5, 4)
        row.setSpacing(7)

        kind = QLabel("▣" if attachment.attachment_type == "image" else "▧")
        kind.setObjectName("attachmentKind")
        kind.setAccessibleName(
            i18n.t(
                "chat.attachment_image"
                if attachment.attachment_type == "image"
                else "chat.attachment_file"
            )
        )

        self._name = QLabel()
        self._name.setObjectName("attachmentName")
        self._name.setTextFormat(Qt.TextFormat.PlainText)
        self._name.setMinimumWidth(0)
        self._name.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        # A filename is useful context; the private source directory never is.
        self._name.setToolTip(attachment.display_name)

        size = QLabel(readable_file_size(attachment.size_bytes))
        size.setObjectName("attachmentSize")

        remove = QPushButton("×")
        remove.setObjectName("attachmentRemove")
        remove.setFixedSize(24, 24)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setToolTip(
            i18n.t("chat.attachment_remove", name=attachment.display_name)
        )
        remove.setAccessibleName(remove.toolTip())
        remove.clicked.connect(
            lambda _checked=False: self.remove_requested.emit(attachment.draft_id)
        )

        row.addWidget(kind)
        row.addWidget(self._name, 1)
        row.addWidget(size)
        row.addWidget(remove)
        self._refresh_elision()

    def _refresh_elision(self) -> None:
        width = max(40, self._name.width())
        self._name.setText(
            self._name.fontMetrics().elidedText(
                self._display_name, Qt.TextElideMode.ElideMiddle, width,
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt method name)
        super().resizeEvent(event)
        self._refresh_elision()


class _DirectiveChip(QFrame):
    """Removable display-only chip for one structured turn modifier."""

    remove_requested = pyqtSignal(str)

    def __init__(self, identity: str, label: str) -> None:
        super().__init__()
        self.setObjectName("directiveChip")
        self.setAccessibleName(label)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 4, 3)
        row.setSpacing(5)
        text = QLabel(label)
        text.setObjectName("directiveChipLabel")
        remove = QPushButton("×")
        remove.setObjectName("directiveChipRemove")
        remove.setFixedSize(22, 22)
        remove.setAccessibleName(
            ("Retirer " if i18n.get_language() == "fr" else "Remove ") + label
        )
        remove.clicked.connect(
            lambda _checked=False: self.remove_requested.emit(identity)
        )
        row.addWidget(text)
        row.addWidget(remove)

@dataclass
class _ComposerDraft:
    text: str = ""
    attachments: list[DraftAttachment] = field(default_factory=list)
    directives: SlashTurnDirectivesV1 = EMPTY_DIRECTIVES


class _ChatPage(QWidget):
    """Scrollable message list + text input row at the bottom."""

    send_text = pyqtSignal(str)
    submit_draft = pyqtSignal(str, object)
    submit_directive_draft = pyqtSignal(str, object, object)
    informational_requested = pyqtSignal(str)
    # Kept as a compatibility signal. Selecting a file no longer emits it.
    pick_file = pyqtSignal(str, str)
    _BUSY_TEXT = "OSIA is still working in this conversation."
    _TOOL_WAITING_STATUSES = frozenset({
        "action_starting", "action_in_progress", "waiting_tool_result",
        "waiting_verification", "verifying", "resuming",
        "cancellation_requested", "blocked_unknown_effect",
    })

    def __init__(self):
        super().__init__()
        self._busy = False
        self._tool_status = ""
        self._draft_context: str | None = None
        self._drafts: dict[str | None, _ComposerDraft] = {}
        self._pending_attachments: list[DraftAttachment] = []
        self._directives = EMPTY_DIRECTIVES
        self._motion = MotionController(
            self,
            reduced_motion=lambda: (
                reduced_motion_requested(self) or not self.isVisible()
            ),
        )
        self._stream = DesktopTextStreamController(
            self,
            reduced_motion=lambda: (
                reduced_motion_requested(self) or not self.isVisible()
            ),
        )
        self._stream.text_changed.connect(self._on_stream_text_changed)
        self._stream_bubble: _Bubble | None = None
        self._stream_wrap: QWidget | None = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
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
        # WOR-48: hide the vertical bar too (scrolling stays fully functional
        # via wheel/drag/trackpad); the QSS below makes it 0px/transparent.
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._messages_host = QWidget()
        self._messages_host.setObjectName("chatHost")
        self._messages_layout = QVBoxLayout(self._messages_host)
        self._messages_layout.setContentsMargins(14, 14, 14, 14)
        self._messages_layout.setSpacing(8)
        self._messages_layout.addStretch(1)
        self._scroll.setWidget(self._messages_host)
        self._syncing_bubble_geometry = False
        self._scroll.viewport().installEventFilter(self)
        outer.addWidget(self._scroll, 1)

        # Empty-state placeholder, swapped out as soon as a message arrives.
        self._empty = self._build_empty_label()
        self._empty_idx = self._messages_layout.count() - 1
        self._messages_layout.insertWidget(self._empty_idx, self._empty)

        # Input row
        input_wrap = QFrame()
        input_wrap.setObjectName("inputWrap")
        input_layout = QVBoxLayout(input_wrap)
        input_layout.setContentsMargins(10, 8, 10, 10)
        input_layout.setSpacing(4)

        self._slash_palette = SlashCommandPalette()
        settings = load_settings() or {}
        self._slash_palette.set_context(
            locale=i18n.get_language(),
            provider_id=str(settings.get("selected_model_provider") or ""),
            model_id=str(settings.get("selected_model") or ""),
        )
        self._slash_palette.item_selected.connect(self._on_slash_selection)
        input_layout.addWidget(self._slash_palette)

        self._notice = QLabel("")
        self._notice.setObjectName("chatNotice")
        self._notice.setWordWrap(True)
        self._notice.hide()
        self._notice_timer.timeout.connect(self._notice.hide)
        input_layout.addWidget(self._notice)

        self._directive_host = QFrame()
        self._directive_host.setObjectName("directiveHost")
        self._directive_layout = QGridLayout(self._directive_host)
        self._directive_layout.setContentsMargins(0, 0, 0, 0)
        self._directive_layout.setHorizontalSpacing(4)
        self._directive_layout.setVerticalSpacing(4)
        self._directive_host.hide()
        input_layout.addWidget(self._directive_host)

        self._attachment_scroll = QScrollArea()
        self._attachment_scroll.setObjectName("attachmentScroll")
        self._attachment_scroll.setWidgetResizable(True)
        self._attachment_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._attachment_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._attachment_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._attachment_scroll.setMaximumHeight(76)
        self._attachment_host = QWidget()
        self._attachment_host.setObjectName("attachmentHost")
        self._attachment_layout = QVBoxLayout(self._attachment_host)
        self._attachment_layout.setContentsMargins(0, 0, 0, 0)
        self._attachment_layout.setSpacing(4)
        self._attachment_scroll.setWidget(self._attachment_host)
        self._attachment_scroll.hide()
        input_layout.addWidget(self._attachment_scroll)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._file_btn = _ComposerButton("attach")
        self._file_btn.setObjectName("iconBtn")
        self._file_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._file_btn.setToolTip(i18n.t("chat.attach_file"))
        self._file_btn.setAccessibleName(i18n.t("chat.attach_file"))
        self._file_btn.clicked.connect(self._on_pick_file)

        self._input = _ComposerInput()
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText(i18n.t("chat.input_placeholder"))
        self._input.setAccessibleName(i18n.t("chat.input_placeholder"))
        self._input.send_requested.connect(self._on_send)
        self._input.palette_key_handler = self._slash_palette.handle_key

        self._send_btn = _ComposerButton("send")
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setToolTip(i18n.t("chat.send"))
        self._send_btn.setAccessibleName(i18n.t("chat.send"))
        self._send_btn.clicked.connect(self._on_send)
        self._input.textChanged.connect(self._on_input_changed)
        self._sync_send_state()

        row.addWidget(self._file_btn)
        row.addWidget(self._input, 1)
        row.addWidget(self._send_btn)
        input_layout.addLayout(row)
        outer.addWidget(input_wrap)

        # Re-translate the input chrome + empty-state label on language change.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

    def _retranslate(self) -> None:
        self._file_btn.setToolTip(i18n.t("chat.attach_file"))
        self._file_btn.setAccessibleName(i18n.t("chat.attach_file"))
        self._apply_composer_state()
        self._input.setAccessibleName(i18n.t("chat.input_placeholder"))
        self._send_btn.setToolTip(i18n.t("chat.send"))
        self._send_btn.setAccessibleName(i18n.t("chat.send"))
        self._rebuild_attachment_chips()
        self._rebuild_directive_chips()
        if not _is_deleted(self._empty):
            self._empty.setText(i18n.t("chat.empty"))

    # ── Public API ────────────────────────────────────────────────────────

    def pending_attachments(self) -> tuple[DraftAttachment, ...]:
        return tuple(self._pending_attachments)

    def set_draft_context(self, conversation_id: str | None) -> None:
        """Save and restore text plus attachments for one conversation."""

        if conversation_id == self._draft_context:
            return
        self._save_current_draft()
        self._draft_context = conversation_id
        settings = load_settings() or {}
        self._slash_palette.set_context(
            locale=i18n.get_language(),
            provider_id=str(settings.get("selected_model_provider") or ""),
            model_id=str(settings.get("selected_model") or ""),
            conversation_id=str(conversation_id or ""),
        )
        self._load_current_draft()

    def bind_unbound_draft(self, conversation_id: str) -> None:
        """Move the pre-first-message draft onto its lazily created chat."""

        if not conversation_id or None not in self._drafts:
            return
        self._save_current_draft()
        unbound = self._drafts.pop(None)
        self._drafts[conversation_id] = unbound
        self._draft_context = conversation_id
        self._load_current_draft()

    def discard_draft(self, conversation_id: str | None) -> None:
        self._drafts.pop(conversation_id, None)
        if self._draft_context == conversation_id:
            self._input.clear()
            self._pending_attachments = []
            self._directives = EMPTY_DIRECTIVES
            self._rebuild_attachment_chips()
            self._rebuild_directive_chips()
            self._sync_send_state()

    def accept_draft_submission(self, attachment_ids) -> bool:
        expected = tuple(item.draft_id for item in self._pending_attachments)
        if expected != tuple(attachment_ids or ()):
            return False
        self._input.clear()
        self._pending_attachments = []
        self._directives = EMPTY_DIRECTIVES
        self._drafts.pop(self._draft_context, None)
        self._rebuild_attachment_chips()
        self._rebuild_directive_chips()
        self._sync_send_state()
        self._input.setFocus()
        return True

    def mark_draft_failure(self, error: DesktopAttachmentError) -> None:
        affected = error.attachment_id
        for item in self._pending_attachments:
            if affected is None or item.draft_id == affected:
                item.validation_state = "error"
                item.error_code = error.code
        self._rebuild_attachment_chips()
        self.show_notice(self._attachment_error_text(error), duration_ms=7000)

    def show_attachment_error(self, error: DesktopAttachmentError) -> None:
        self.mark_draft_failure(error)

    def _save_current_draft(self) -> None:
        text = self._input.toPlainText()
        if text or self._pending_attachments or not self._directives.is_empty:
            self._drafts[self._draft_context] = _ComposerDraft(
                text=text,
                attachments=list(self._pending_attachments),
                directives=self._directives,
            )
        else:
            self._drafts.pop(self._draft_context, None)

    def _load_current_draft(self) -> None:
        draft = self._drafts.get(self._draft_context, _ComposerDraft())
        self._input.blockSignals(True)
        self._input.setPlainText(draft.text)
        self._input.blockSignals(False)
        self._pending_attachments = list(draft.attachments)
        self._directives = draft.directives
        if self._directives.model_override is not None:
            self._slash_palette.set_context(
                locale=i18n.get_language(),
                provider_id=self._directives.model_override.provider_id,
                model_id=self._directives.model_override.model_id,
                conversation_id=str(self._draft_context or ""),
            )
        self._rebuild_attachment_chips()
        self._rebuild_directive_chips()
        self._sync_send_state()

    def accept_directive_submission(self, attachment_ids=()) -> bool:
        expected = tuple(item.draft_id for item in self._pending_attachments)
        if expected != tuple(attachment_ids or ()):
            return False
        self._input.clear()
        self._pending_attachments = []
        self._directives = EMPTY_DIRECTIVES
        self._drafts.pop(self._draft_context, None)
        self._rebuild_attachment_chips()
        self._rebuild_directive_chips()
        self._sync_send_state()
        self._input.setFocus()
        return True

    def _on_input_changed(self) -> None:
        self._slash_palette.update_from_text(
            self._input.toPlainText(), enabled=not self._busy,
        )
        self._sync_send_state()

    def _on_slash_selection(self, row: dict) -> None:
        handler = str(row.get("handler_contract") or "")
        selection_type = str(row.get("selection_type") or "")
        alias = str(row.get("canonical_alias") or "")
        next_directives = self._directives
        if handler in {
            "show_safe_tools_v1",
            "show_safe_status_v1",
            "show_available_help_v1",
        }:
            command_id = str(row.get("id") or "")
            remaining = consume_palette_prefix(self._input.text())
            if not self._directives.is_empty or remaining.strip():
                self.show_notice(
                    "Les commandes d’information doivent être utilisées seules."
                    if i18n.get_language() == "fr" else
                    "Informational commands must be used alone."
                )
                self._slash_palette.close_palette()
                return
            self._input.setPlainText(remaining)
            self.informational_requested.emit(command_id)
            return
        if handler == "set_current_web_requirement_v1":
            next_directives = replace(
                next_directives,
                capability_requirements=CapabilityRequirements(current_web=True),
            )
        elif handler == "select_connector_hint_v1":
            next_directives = replace(
                next_directives,
                connector_hints=(ConnectorHint(str(row["connector_id"])),),
            )
        elif selection_type == "model":
            alias = "model"
            if row.get("remove_override") is True:
                next_directives = replace(next_directives, model_override=None)
                settings = load_settings() or {}
                self._slash_palette.set_context(
                    locale=i18n.get_language(),
                    provider_id=str(settings.get("selected_model_provider") or ""),
                    model_id=str(settings.get("selected_model") or ""),
                    conversation_id=str(self._draft_context or ""),
                )
            else:
                next_directives = replace(
                    next_directives,
                    model_override=ModelOverride(
                        str(row["provider_id"]), str(row["model_id"]),
                    ),
                )
                self._slash_palette.set_context(
                    locale=i18n.get_language(),
                    provider_id=str(row["provider_id"]),
                    model_id=str(row["model_id"]),
                    conversation_id=str(self._draft_context or ""),
                )
        elif selection_type == "conversation":
            alias = "conversation"
            next_directives = replace(
                next_directives,
                conversation_refs=(ConversationRef(
                    str(row["conversation_id"]),
                    str(row.get("display_title") or "")[:120],
                    str(row.get("display_project") or "")[:80],
                ),),
            )
        elif selection_type == "reasoning":
            alias = "reasoning"
            next_directives = replace(
                next_directives,
                reasoning_override=str(row["reasoning_level"]),
            )
        else:
            return
        try:
            next_directives._validate_bounds()
        except SlashDirectiveError as error:
            self.show_notice(error.message(i18n.get_language()))
            return
        self._directives = next_directives
        self._input.setPlainText(consume_palette_prefix(self._input.text()))
        self._rebuild_directive_chips()
        self._sync_send_state()
        self._input.setFocus()

    def _directive_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        if self._directives.connector_hints:
            value = self._directives.connector_hints[0].connector_id
            rows.append(("connector", "/gmail" if value == "gmail-rest" else f"/{value}"))
        if self._directives.capability_requirements.current_web:
            rows.append(("web", "/web"))
        if self._directives.conversation_refs:
            ref = self._directives.conversation_refs[0]
            rows.append(("conversation", ref.display_title or "/conversation"))
        if self._directives.model_override is not None:
            rows.append(("model", self._directives.model_override.model_id))
        if self._directives.reasoning_override is not None:
            rows.append(("reasoning", self._directives.reasoning_override))
        return rows

    def _rebuild_directive_chips(self) -> None:
        while self._directive_layout.count():
            item = self._directive_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, (identity, label) in enumerate(self._directive_rows()):
            chip = _DirectiveChip(identity, label)
            chip.remove_requested.connect(self._remove_directive)
            self._directive_layout.addWidget(chip, index // 2, index % 2)
        self._directive_host.setVisible(not self._directives.is_empty)

    def _remove_directive(self, identity: str) -> None:
        changes = {
            "connector": {"connector_hints": ()},
            "web": {"capability_requirements": CapabilityRequirements()},
            "conversation": {"conversation_refs": ()},
            "model": {"model_override": None},
            "reasoning": {"reasoning_override": None},
        }.get(identity)
        if changes is None:
            return
        self._directives = replace(self._directives, **changes)
        if identity == "model":
            settings = load_settings() or {}
            self._slash_palette.set_context(
                locale=i18n.get_language(),
                provider_id=str(settings.get("selected_model_provider") or ""),
                model_id=str(settings.get("selected_model") or ""),
                conversation_id=str(self._draft_context or ""),
            )
        self._rebuild_directive_chips()
        self._sync_send_state()

    def _rebuild_attachment_chips(self) -> None:
        while self._attachment_layout.count():
            layout_item = self._attachment_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.deleteLater()
        for attachment in self._pending_attachments:
            chip = _AttachmentChip(attachment)
            chip.remove_requested.connect(self._remove_attachment)
            self._attachment_layout.addWidget(chip)
        self._attachment_scroll.setVisible(bool(self._pending_attachments))

    def _remove_attachment(self, draft_id: str) -> None:
        self._pending_attachments = [
            item for item in self._pending_attachments
            if item.draft_id != draft_id
        ]
        self._rebuild_attachment_chips()
        self._sync_send_state()
        self._input.setFocus()

    @staticmethod
    def _attachment_error_text(error: DesktopAttachmentError) -> str:
        key = {
            "missing_file": "chat.attachment_error_missing",
            "not_regular_file": "chat.attachment_error_regular",
            "empty_file": "chat.attachment_error_empty",
            "unreadable_file": "chat.attachment_error_unreadable",
            "changed_file": "chat.attachment_error_changed",
            "unsupported_provider": "chat.attachment_error_provider",
            "unsupported_model": "chat.attachment_error_model",
            "unsupported_mime": "chat.attachment_error_mime",
            "provider_size_limit": "chat.attachment_error_size",
            "too_many_images": "chat.attachment_error_count",
            "resource_limit": "chat.attachment_error_resources",
        }.get(error.code, "chat.attachment_error_invalid")
        return i18n.t(
            key,
            name=error.display_name or i18n.t("chat.attachment_file"),
            provider=error.provider or "",
            model=error.model or "",
        )

    def clear_messages(self) -> None:
        self._stream.clear()
        self._stream_bubble = None
        self._stream_wrap = None
        self._motion.settle()
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
            self._scroll_timer.start(0)
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
        self._scroll_timer.start(0)

    def append_assistant_stream_chunk(self, text: str) -> None:
        """Extend the active assistant target with one cleaned sentence chunk."""
        self._stream.append_chunk(_display_text(text), separator=" ")

    def finalize_assistant_stream(self, text: str) -> None:
        """Synchronously replace the presentation with the canonical final."""
        final = _display_text(text)
        if not final:
            if self._stream.active or self._stream_bubble is not None:
                self._stream.finalize("")
                self._discard_stream_bubble()
            return
        if not self._stream.active and self._stream_bubble is None:
            self.append_message("assistant", final)
            return
        self._stream.finalize(final)
        self._mark_stream_finished()

    def fail_assistant_stream(self, text: str) -> None:
        error = _display_text(text)
        if not error:
            self.cancel_assistant_stream(flush=True)
            return
        if not self._stream.active and self._stream_bubble is None:
            self.append_message("assistant", error)
            return
        self._stream.error(error)
        self._mark_stream_finished(error=True)

    def cancel_assistant_stream(self, *, flush: bool = True) -> None:
        self._stream.cancel(flush=flush)
        self._mark_stream_finished()

    # ── Internals ─────────────────────────────────────────────────────────

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        if not self._busy:
            self._tool_status = ""
        self._apply_composer_state()
        self._sync_send_state()

    def set_tool_status(self, status: str) -> None:
        safe = str(status or "")
        self._tool_status = (
            safe if safe in self._TOOL_WAITING_STATUSES else ""
        )
        if self._tool_status:
            self._busy = True
        self._apply_composer_state()
        self._sync_send_state()

    def _apply_composer_state(self) -> None:
        waiting = bool(
            self._busy and self._tool_status in self._TOOL_WAITING_STATUSES
        )
        self._input.setEnabled(not self._busy or waiting)
        self._file_btn.setEnabled(not self._busy)
        if self._busy:
            self._slash_palette.close_palette()
        key = {
            "action_starting": "chat.tool_action_starting",
            "action_in_progress": "chat.tool_action_in_progress",
            "waiting_tool_result": "chat.tool_waiting",
            "waiting_verification": "chat.tool_waiting",
            "verifying": "chat.tool_verifying",
            "resuming": "chat.tool_resuming",
            "cancellation_requested": "chat.tool_cancelling",
            "blocked_unknown_effect": "chat.tool_blocked",
        }.get(self._tool_status)
        if key:
            label = i18n.t(key)
            self._input.setPlaceholderText(label)
            self._notice.setText(label)
            self._notice.show()
        else:
            self._input.setPlaceholderText(
                self._BUSY_TEXT if self._busy else i18n.t("chat.input_placeholder")
            )
            if not self._busy:
                self._notice.hide()

    def _sync_send_state(self, _text: str | None = None) -> None:
        text = self._input.toPlainText().strip()
        waiting = bool(
            self._busy and self._tool_status in self._TOOL_WAITING_STATUSES
        )
        enabled = (
            (not self._busy and bool(text or self._pending_attachments))
            or (waiting and bool(text) and len(text) <= 160)
        )
        self._send_btn.setEnabled(enabled)
        self._send_btn.setCursor(
            Qt.CursorShape.ArrowCursor
            if not enabled else Qt.CursorShape.PointingHandCursor
        )

    def show_notice(self, text: str, *, duration_ms: int = 2600) -> None:
        self._notice.setText(text)
        self._notice.show()
        self._motion.fade_in(self._notice, MOTION.state_ms)
        self._notice_timer.start(max(0, int(duration_ms)))

    @staticmethod
    def _build_empty_label() -> QLabel:
        label = QLabel(i18n.t("chat.empty"))
        label.setObjectName("chatEmpty")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    def _append_bubble(self, role: str, text: str) -> tuple[QWidget, _Bubble] | None:
        # Defense in depth: never create a bubble (or its wrapper row
        # widget) for empty text — a blank QFrame collapses into a thin
        # violet line in the chat.
        if not text or not str(text).strip():
            return None
        bubble = _Bubble(role, text)
        bubble.set_available_width(self._maximum_bubble_width())
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
        self._fit_messages_host_to_viewport()
        self._motion.fade_in(wrap, MOTION.appearance_ms)
        return wrap, bubble

    def _maximum_bubble_width(self) -> int:
        # The viewport already excludes its frame and any visible scrollbar.
        # The history margins are the only horizontal space between it and a
        # bubble row. Bubble padding stays inside this returned maximum.
        margins = self._messages_layout.contentsMargins()
        usable = (
            self._scroll.viewport().width()
            - margins.left()
            - margins.right()
        )
        return max(1, min(_BUBBLE_MAX_WIDTH, usable))

    def _fit_messages_host_to_viewport(self) -> None:
        self._messages_layout.invalidate()
        self._messages_layout.activate()
        viewport_width = self._scroll.viewport().width()
        if self._messages_host.width() != viewport_width:
            self._messages_host.resize(
                viewport_width, self._messages_host.height()
            )
        self._messages_host.updateGeometry()

    def _sync_bubble_geometry(self) -> None:
        if self._syncing_bubble_geometry:
            return
        self._syncing_bubble_geometry = True
        try:
            maximum = self._maximum_bubble_width()
            for bubble in self._messages_host.findChildren(_Bubble):
                bubble.set_available_width(maximum)
            self._fit_messages_host_to_viewport()
        finally:
            self._syncing_bubble_geometry = False

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt method name)
        if (
            watched is self._scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            self._sync_bubble_geometry()
        return super().eventFilter(watched, event)

    def _on_stream_text_changed(self, text: str) -> None:
        if self._stream_bubble is None:
            if not text:
                return
            try:
                self._empty.hide()
            except RuntimeError:
                return
            created = self._append_bubble("assistant", text)
            if created is None:
                return
            self._stream_wrap, self._stream_bubble = created
            self._stream_bubble.setProperty("streaming", True)
            self._stream_bubble.style().unpolish(self._stream_bubble)
            self._stream_bubble.style().polish(self._stream_bubble)
        else:
            self._stream_bubble.set_text(text)
        self._scroll_to_bottom()

    def _discard_stream_bubble(self) -> None:
        wrap = self._stream_wrap
        self._stream_bubble = None
        self._stream_wrap = None
        if wrap is not None:
            self._messages_layout.removeWidget(wrap)
            wrap.deleteLater()
        if self._messages_layout.count() <= 2:
            self._empty.show()
        self._scroll_to_bottom()

    def _mark_stream_finished(self, *, error: bool = False) -> None:
        bubble = self._stream_bubble
        if bubble is not None:
            bubble.setProperty("streaming", False)
            bubble.setProperty("error", error)
            bubble.style().unpolish(bubble)
            bubble.style().polish(bubble)
        self._stream_bubble = None
        self._stream_wrap = None
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_send(self) -> None:
        waiting = bool(
            self._busy and self._tool_status in self._TOOL_WAITING_STATUSES
        )
        if self._busy and not waiting:
            self.show_notice(self._BUSY_TEXT)
            return
        text = escaped_literal_text(self._input.toPlainText()).strip()
        if waiting and (not text or len(text) > 160):
            self.show_notice(i18n.t("chat.tool_command_limit"))
            self._sync_send_state()
            return
        if not text and not self._pending_attachments:
            self._sync_send_state()
            return
        if waiting:
            # A waiting-turn command is text-only.  Preserve any draft files
            # for the next normal turn, but never attach them to lifecycle
            # controls such as status/resume/cancel.
            self._input.clear()
            self.send_text.emit(text)
            return
        if not self._directives.is_empty:
            try:
                self._directives.require_sendable(
                    body=text,
                    has_attachment=bool(self._pending_attachments),
                )
                self._directives._validate_bounds()
            except SlashDirectiveError as error:
                self.show_notice(error.message(i18n.get_language()))
                return
            for item in self._pending_attachments:
                item.validation_state = "pending"
                item.error_code = None
            self._rebuild_attachment_chips()
            self.submit_directive_draft.emit(
                text,
                list(self._pending_attachments),
                self._directives.to_wire(),
            )
            self._sync_send_state()
            return
        if self._pending_attachments:
            for item in self._pending_attachments:
                item.validation_state = "pending"
                item.error_code = None
            self._rebuild_attachment_chips()
            self.submit_draft.emit(text, list(self._pending_attachments))
            self._sync_send_state()
            return
        self._input.clear()
        self.send_text.emit(text)

    def _on_pick_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, i18n.t("chat.attach_file")
        )
        if not paths:
            self._input.setFocus()
            return
        next_order = 0
        if self._pending_attachments:
            next_order = max(
                item.logical_order for item in self._pending_attachments
            ) + 1
        for path in paths:
            try:
                attachment = stage_attachment(path, next_order)
            except DesktopAttachmentError as error:
                self.show_notice(
                    self._attachment_error_text(error), duration_ms=7000,
                )
                continue
            self._pending_attachments.append(attachment)
            next_order += 1
        self._rebuild_attachment_chips()
        self._sync_send_state()
        self._input.setFocus()


# ── Conversations panel ────────────────────────────────────────────────────

class _ConvRow(QFrame):
    """One row in the conversations list."""

    open_clicked = pyqtSignal(str)
    rename_clicked = pyqtSignal(str, str)   # (conv_id, new_title)
    delete_clicked = pyqtSignal(str)
    move_clicked = pyqtSignal(str, object)

    def __init__(
        self,
        conv: dict,
        is_active: bool = False,
        *,
        folders: list[dict] | None = None,
        indented: bool = False,
    ):
        super().__init__()
        self.setObjectName("convRow")
        self.setProperty("active", is_active)
        self._conv_id = conv.get("id", "")
        self._folder_id = conv.get("folder_id")
        self._folders = list(folders or [])

        row = QHBoxLayout(self)
        row.setContentsMargins(28 if indented else 16, 10, 10, 10)
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

        rename_btn = QPushButton("✎")
        rename_btn.setObjectName("convRename")
        rename_btn.setFixedSize(26, 26)
        rename_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rename_btn.setToolTip(i18n.t("conversations.rename"))
        rename_btn.clicked.connect(self._prompt_rename)

        move_btn = QPushButton("↳")
        move_btn.setObjectName("convRename")
        move_btn.setFixedSize(26, 26)
        move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        move_btn.setToolTip(i18n.t("conversations.move_to_folder"))
        move_btn.clicked.connect(self._show_move_menu)

        del_btn = QPushButton("✕")
        del_btn.setObjectName("convDel")
        del_btn.setFixedSize(26, 26)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(lambda: self.delete_clicked.emit(self._conv_id))

        row.addLayout(title_col, 1)
        row.addWidget(open_btn)
        row.addWidget(move_btn)
        row.addWidget(rename_btn)
        row.addWidget(del_btn)

    def _show_move_menu(self) -> None:
        menu = QMenu(self)
        ungrouped = menu.addAction(
            i18n.t("conversations.remove_from_project")
        )
        ungrouped.setCheckable(True)
        ungrouped.setChecked(not self._folder_id)
        ungrouped.triggered.connect(
            lambda _checked=False: self.move_clicked.emit(self._conv_id, None)
        )
        if self._folders:
            menu.addSeparator()
        for folder in self._folders:
            folder_id = str(folder.get("id") or "")
            action = menu.addAction(str(folder.get("name") or ""))
            action.setCheckable(True)
            action.setChecked(folder_id == self._folder_id)
            action.triggered.connect(
                lambda _checked=False, fid=folder_id: self.move_clicked.emit(
                    self._conv_id, fid,
                )
            )
        menu.exec(self.mapToGlobal(self.rect().bottomRight()))

    def _prompt_rename(self) -> None:
        # Prompt for a new title, pre-filled with the current one. Emit only on
        # accept with a non-empty trimmed value; the store layer trims + caps
        # again, so this is just an early UX guard against blank renames.
        new_title, ok = QInputDialog.getText(
            self,
            i18n.t("conversations.rename_title"),
            i18n.t("conversations.rename_prompt"),
            text=self._full_title,
        )
        if not ok:
            return
        new_title = new_title.strip()
        if not new_title:
            return
        self.rename_clicked.emit(self._conv_id, new_title)

    def resizeEvent(self, event):
        fm = self._title_label.fontMetrics()
        self._title_label.setText(
            fm.elidedText(
                self._full_title, Qt.TextElideMode.ElideRight,
                max(0, self._title_label.width()),
            )
        )
        super().resizeEvent(event)


class _FolderRow(QFrame):
    """Visually distinct canonical folder header and its compact settings."""

    collapsed_changed = pyqtSignal(str, bool)
    new_conversation_clicked = pyqtSignal(str)
    rename_clicked = pyqtSignal(str, str)
    context_policy_changed = pyqtSignal(str, bool)
    reorder_clicked = pyqtSignal(str, int)
    delete_clicked = pyqtSignal(str, int)

    def __init__(self, folder: dict, count: int, index: int, total: int):
        super().__init__()
        self.setObjectName("folderRow")
        self._folder = dict(folder)
        self._folder_id = str(folder.get("id") or "")
        self._folder_name = str(folder.get("name") or "")
        self._collapsed = bool(folder.get("collapsed", False))

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 8, 8)
        row.setSpacing(7)
        toggle = QPushButton("▸" if self._collapsed else "▾")
        toggle.setObjectName("folderToggle")
        toggle.setFixedSize(24, 24)
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.clicked.connect(
            lambda: self.collapsed_changed.emit(
                self._folder_id, not self._collapsed,
            )
        )
        title = QLabel(f"📁  {self._folder_name}")
        title.setObjectName("folderTitle")
        title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        count_label = QLabel(str(count))
        count_label.setObjectName("folderCount")
        settings = QPushButton("⋯")
        settings.setObjectName("folderMenu")
        settings.setFixedSize(28, 26)
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.clicked.connect(
            lambda: self._show_settings(settings, index, total)
        )
        row.addWidget(toggle)
        row.addWidget(title, 1)
        row.addWidget(count_label)
        row.addWidget(settings)

    def _show_settings(self, anchor: QWidget, index: int, total: int) -> None:
        menu = QMenu(self)
        new_chat = menu.addAction(i18n.t("conversations.new_chat"))
        new_chat.triggered.connect(
            lambda: self.new_conversation_clicked.emit(self._folder_id)
        )
        rename = menu.addAction(i18n.t("conversations.rename_folder"))
        rename.triggered.connect(self._prompt_rename)
        settings_menu = menu.addMenu(i18n.t("conversations.folder_settings"))
        allow = settings_menu.addAction(
            i18n.t("conversations.allow_folder_context")
        )
        allow.setCheckable(True)
        allow.setChecked(bool(self._folder.get("allow_folder_context", False)))
        allow.triggered.connect(
            lambda checked: self.context_policy_changed.emit(
                self._folder_id, bool(checked),
            )
        )
        menu.addSeparator()
        move_up = menu.addAction(i18n.t("conversations.move_folder_up"))
        move_up.setEnabled(index > 0)
        move_up.triggered.connect(
            lambda: self.reorder_clicked.emit(self._folder_id, -1)
        )
        move_down = menu.addAction(i18n.t("conversations.move_folder_down"))
        move_down.setEnabled(index + 1 < total)
        move_down.triggered.connect(
            lambda: self.reorder_clicked.emit(self._folder_id, 1)
        )
        menu.addSeparator()
        delete = menu.addAction(i18n.t("conversations.delete_project"))
        delete.triggered.connect(self._confirm_delete)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomRight()))

    def _confirm_delete(self) -> None:
        snapshot = conversations.conversation_snapshot()
        folder = next(
            (
                item
                for item in snapshot.get("folders") or []
                if str(item.get("id") or "") == self._folder_id
            ),
            None,
        )
        if folder is None:
            return
        count = sum(
            1
            for item in snapshot.get("items") or []
            if item.get("folder_id") == self._folder_id
        )
        confirm_key = (
            "conversations.delete_project_confirm_one"
            if count == 1
            else "conversations.delete_project_confirm"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(i18n.t("conversations.delete_project"))
        box.setText(i18n.t(
            confirm_key,
            name=str(folder.get("name") or self._folder_name),
            count=count,
        ))
        delete_button = box.addButton(
            i18n.t("deletion.confirm.allow"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = box.addButton(
            i18n.t("deletion.confirm.cancel"),
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(cancel_button)
        box.exec()
        if box.clickedButton() is delete_button:
            self.delete_clicked.emit(self._folder_id, count)

    def _prompt_rename(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            i18n.t("conversations.rename_folder"),
            i18n.t("conversations.folder_name"),
            text=self._folder_name,
        )
        name = name.strip()
        if accepted and name and name != self._folder_name:
            self.rename_clicked.emit(self._folder_id, name)


class _ConversationsPanel(QWidget):
    """List of past conversations + 'new chat' action."""

    new_conversation = pyqtSignal()
    open_conversation = pyqtSignal(str)
    rename_conversation = pyqtSignal(str, str)
    delete_conversation = pyqtSignal(str)
    folder_mutation_requested = pyqtSignal(dict)
    bulk_delete_ungrouped_requested = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._active_id: str | None = None
        self._last_signature = None

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
        self._more_btn = QPushButton("⋯")
        self._more_btn.setObjectName("convMore")
        self._more_btn.setFixedSize(30, 30)
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.clicked.connect(self._show_more_menu)
        header.addWidget(self._title)
        header.addStretch()
        header.addWidget(self._new_btn)
        header.addWidget(self._more_btn)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setVerticalScrollBarPolicy(
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
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(750)
        self._poll_timer.timeout.connect(self._poll_canonical_store)
        self._poll_timer.start()

    def _retranslate(self) -> None:
        self._title.setText(i18n.t("conversations.title"))
        self._new_btn.setText("＋ " + i18n.t("conversations.new_chat"))
        self.refresh()

    def set_active(self, conv_id: str | None) -> None:
        self._active_id = conv_id
        self.refresh()

    @staticmethod
    def _snapshot_signature(snapshot: dict):
        return (
            int(snapshot.get("snapshot_revision") or 0),
            tuple(
                (
                    folder.get("id"), folder.get("name"), folder.get("order"),
                    bool(folder.get("collapsed", False)),
                    bool(folder.get("allow_folder_context", False)),
                )
                for folder in snapshot.get("folders") or []
            ),
            tuple(
                (
                    item.get("id"), item.get("title"), item.get("updated_at"),
                    item.get("folder_id"),
                )
                for item in snapshot.get("items") or []
            ),
        )

    def _poll_canonical_store(self) -> None:
        if not self.isVisible():
            return
        snapshot = conversations.conversation_snapshot()
        if self._snapshot_signature(snapshot) != self._last_signature:
            self.refresh(snapshot=snapshot)

    def _show_more_menu(self) -> None:
        menu = QMenu(self)
        create = menu.addAction(i18n.t("conversations.create_folder"))
        create.triggered.connect(self._prompt_create_folder)
        delete_all = menu.addAction(
            i18n.t("conversations.delete_all_ungrouped")
        )
        delete_all.triggered.connect(self._confirm_bulk_delete)
        menu.exec(self._more_btn.mapToGlobal(self._more_btn.rect().bottomRight()))

    def _prompt_create_folder(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            i18n.t("conversations.create_folder"),
            i18n.t("conversations.folder_name"),
        )
        name = name.strip()
        if accepted and name:
            self.folder_mutation_requested.emit({
                "action": "create",
                "name": name,
            })

    def _confirm_bulk_delete(self) -> None:
        snapshot = conversations.conversation_snapshot()
        affected = sum(
            1 for item in snapshot.get("items") or []
            if not item.get("folder_id")
        )
        if affected <= 0:
            QMessageBox.information(
                self,
                i18n.t("conversations.delete_all_ungrouped"),
                i18n.t("conversations.bulk_delete_none"),
            )
            return
        phrase = f"DELETE {affected}"
        typed, accepted = QInputDialog.getText(
            self,
            i18n.t("conversations.bulk_delete_title"),
            i18n.t(
                "conversations.bulk_delete_confirm",
                count=affected,
                phrase=phrase,
            ),
        )
        if accepted and typed.strip() == phrase:
            self.bulk_delete_ungrouped_requested.emit(affected)

    def _emit_folder_state(self, folder_id: str, collapsed: bool) -> None:
        self.folder_mutation_requested.emit({
            "action": "collapse",
            "folder_id": folder_id,
            "collapsed": collapsed,
        })

    def _emit_new_in_folder(self, folder_id: str) -> None:
        self.folder_mutation_requested.emit({
            "action": "new_conversation",
            "folder_id": folder_id,
        })

    def _emit_delete_folder(self, folder_id: str, expected_count: int) -> None:
        self.folder_mutation_requested.emit({
            "action": "delete",
            "folder_id": folder_id,
            "expected_count": expected_count,
        })

    def _emit_folder_rename(self, folder_id: str, name: str) -> None:
        self.folder_mutation_requested.emit({
            "action": "rename",
            "folder_id": folder_id,
            "name": name,
        })

    def _emit_folder_context(self, folder_id: str, allow: bool) -> None:
        self.folder_mutation_requested.emit({
            "action": "context",
            "folder_id": folder_id,
            "allow": allow,
        })

    def _emit_folder_reorder(self, folder_id: str, delta: int) -> None:
        ids = [
            str(folder.get("id") or "")
            for folder in conversations.list_folders()
        ]
        try:
            index = ids.index(folder_id)
        except ValueError:
            return
        target = index + int(delta)
        if target < 0 or target >= len(ids):
            return
        ids[index], ids[target] = ids[target], ids[index]
        self.folder_mutation_requested.emit({
            "action": "reorder",
            "folder_ids": ids,
        })

    def _emit_move(self, conv_id: str, folder_id) -> None:
        self.folder_mutation_requested.emit({
            "action": "move",
            "conversation_id": conv_id,
            "folder_id": folder_id,
        })

    def refresh(self, *, snapshot: dict | None = None) -> None:
        # Wipe & rebuild — the list is small so we don't bother diffing.
        while self._host_layout.count() > 1:
            item = self._host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        snapshot = snapshot or conversations.conversation_snapshot()
        self._last_signature = self._snapshot_signature(snapshot)
        items = list(snapshot.get("items") or [])
        folders = list(snapshot.get("folders") or [])
        if not items and not folders:
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
        insert_at = 0

        def add_conversation(conv: dict, *, indented: bool) -> None:
            nonlocal insert_at
            row = _ConvRow(
                conv,
                is_active=(conv.get("id") == self._active_id),
                folders=folders,
                indented=indented,
            )
            row.open_clicked.connect(self.open_conversation.emit)
            row.rename_clicked.connect(self.rename_conversation.emit)
            row.delete_clicked.connect(self.delete_conversation.emit)
            row.move_clicked.connect(self._emit_move)
            self._host_layout.insertWidget(insert_at, row)
            insert_at += 1

        if folders:
            label = QLabel(i18n.t("conversations.projects"))
            label.setObjectName("folderSectionLabel")
            self._host_layout.insertWidget(insert_at, label)
            insert_at += 1
        for index, folder in enumerate(folders):
            folder_id = str(folder.get("id") or "")
            members = [
                item for item in items if item.get("folder_id") == folder_id
            ]
            folder_row = _FolderRow(folder, len(members), index, len(folders))
            folder_row.collapsed_changed.connect(self._emit_folder_state)
            folder_row.new_conversation_clicked.connect(
                self._emit_new_in_folder
            )
            folder_row.rename_clicked.connect(self._emit_folder_rename)
            folder_row.context_policy_changed.connect(self._emit_folder_context)
            folder_row.reorder_clicked.connect(self._emit_folder_reorder)
            folder_row.delete_clicked.connect(self._emit_delete_folder)
            self._host_layout.insertWidget(insert_at, folder_row)
            insert_at += 1
            if not folder.get("collapsed", False):
                for conv in members:
                    add_conversation(conv, indented=True)

        ungrouped = [item for item in items if not item.get("folder_id")]
        if folders and ungrouped:
            label = QLabel(i18n.t("conversations.ungrouped"))
            label.setObjectName("folderSectionLabel")
            self._host_layout.insertWidget(insert_at, label)
            insert_at += 1
        for conv in ungrouped:
            add_conversation(conv, indented=False)


# ── Model panel ─────────────────────────────────────────────────────────────

class _ModelRow(QFrame):
    """One model entry — provider tag, label, select button."""

    selected = pyqtSignal(str)

    def __init__(
        self, model: dict, is_active: bool,
        reasoning_options: list[dict] | None = None,
    ):
        super().__init__()
        self.setObjectName("modelRow")
        self.setProperty("active", is_active)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._mid = model["id"]
        self._provider = model.get("provider", "")
        self._name_text = str(model.get("label") or model["id"])
        self._sub_text = str(model.get("id", ""))
        self._reasoning_chip_text = ""
        if reasoning_options is not None:
            self._reasoning_options = list(reasoning_options)
        else:
            try:
                self._reasoning_options = get_reasoning_options(
                    self._provider, self._mid
                )
            except Exception:
                self._reasoning_options = []

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 10, 8)
        row.setSpacing(8)

        prov = QLabel(model.get("provider", "").upper()[:4])
        prov.setObjectName("modelProv")
        prov.setFixedWidth(48)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        name = QLabel(self._name_text)
        name.setObjectName("modelName")
        name.setWordWrap(False)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sub = QLabel(self._sub_text)
        sub.setObjectName("modelSub")
        sub.setWordWrap(False)
        sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._reasoning_chip = QLabel()
        self._reasoning_chip.setObjectName("modelReasoningChip")
        self._reasoning_chip.setWordWrap(False)
        self._reasoning_chip.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._reasoning_chip.setVisible(False)
        self._name_label = name
        self._sub_label = sub
        col.addWidget(name)
        col.addWidget(sub)
        col.addWidget(self._reasoning_chip)

        btn = QPushButton(
            "✓ " + i18n.t("models.active") if is_active else i18n.t("models.use")
        )
        btn.setObjectName("modelUse")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setEnabled(not is_active)
        btn.clicked.connect(lambda: self.selected.emit(self._mid))
        btn_width = max(68, btn.fontMetrics().horizontalAdvance(btn.text()) + 34)
        btn.setFixedWidth(btn_width)

        row.addWidget(prov)
        row.addLayout(col, 1)
        self._reasoning_btn = None
        actions = QWidget()
        actions_lay = QHBoxLayout(actions)
        actions_lay.setContentsMargins(0, 0, 0, 0)
        actions_lay.setSpacing(8)
        actions_width = btn_width
        if self._reasoning_options:
            self._reasoning_btn = QPushButton("\u2699")
            self._reasoning_btn.setObjectName("modelReasoning")
            self._reasoning_btn.setFixedSize(30, 30)
            self._reasoning_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._reasoning_btn.setToolTip(i18n.t("reasoning.tooltip"))
            self._reasoning_btn.clicked.connect(self._show_reasoning_menu)
            actions_lay.addWidget(self._reasoning_btn)
            actions_width += 38
            i18n.add_listener(self._retranslate_reasoning)
            self.destroyed.connect(
                lambda: i18n.remove_listener(self._retranslate_reasoning)
            )
        actions_lay.addWidget(btn)
        actions.setFixedWidth(actions_width)
        actions.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        row.addWidget(actions)
        self._refresh_reasoning_chip()

    def _refresh_elided_text(self) -> None:
        for label, full_text in (
            (self._name_label, self._name_text),
            (self._sub_label, self._sub_text),
            (self._reasoning_chip, self._reasoning_chip_text),
        ):
            label.setText(
                label.fontMetrics().elidedText(
                    full_text,
                    Qt.TextElideMode.ElideRight,
                    max(0, label.width()),
                )
            )

    def resizeEvent(self, event):
        self._refresh_elided_text()
        super().resizeEvent(event)

    def _option_label(self, option: dict) -> str:
        key = option.get("label_key")
        if key:
            return i18n.t(str(key))
        return str(option.get("label") or option.get("id") or "")

    def _explicit_reasoning_option_id(self) -> str:
        option_ids = {
            str(opt.get("id") or "").strip().lower()
            for opt in self._reasoning_options
        }
        try:
            cfg = get_reasoning_settings()
            model_key = (
                f"chatgpt_account:{self._mid}"
                if self._provider == "chatgpt_account" else self._mid
            )
            selected = (
                (cfg.get("models") or {}).get(model_key, "auto")
                if isinstance(cfg, dict) else "auto"
            )
        except Exception:
            selected = "auto"
        selected = str(selected or "auto").strip().lower()
        return selected if selected in option_ids else "auto"

    def _refresh_reasoning_chip(self) -> None:
        if not self._reasoning_options:
            self._reasoning_chip.setVisible(False)
            return
        selected = self._explicit_reasoning_option_id()
        option = next(
            (
                opt for opt in self._reasoning_options
                if str(opt.get("id") or "").strip().lower() == selected
            ),
            None,
        )
        show = bool(option and not option.get("default"))
        if show:
            label = self._option_label(option)
            self._reasoning_chip_text = i18n.t("reasoning.current_level", level=label)
        else:
            self._reasoning_chip_text = ""
        self._reasoning_chip.setVisible(show)
        if self._reasoning_btn is not None:
            self._reasoning_btn.setProperty("selected", show)
            self._reasoning_btn.style().unpolish(self._reasoning_btn)
            self._reasoning_btn.style().polish(self._reasoning_btn)
        self._refresh_elided_text()

    def _show_reasoning_menu(self) -> None:
        if not self._reasoning_options or self._reasoning_btn is None:
            return
        current = self._explicit_reasoning_option_id()
        menu = QMenu(self)
        menu.setObjectName("reasoningMenu")
        menu.setStyleSheet(themed_stylesheet("""
            QMenu#reasoningMenu {
                background: #0d0b12;
                border: 1px solid rgba(196, 181, 245, 0.28);
                color: #e8e6f0;
                padding: 6px;
            }
            QMenu#reasoningMenu::item {
                padding: 7px 18px;
                border-radius: 6px;
                font-size: 12px;
            }
            QMenu#reasoningMenu::item:selected {
                background: rgba(196, 181, 245, 0.20);
                color: #ffffff;
            }
        """, get_theme_manager().palette))
        title = menu.addAction(i18n.t("settings.reasoning"))
        title.setEnabled(False)
        menu.addSeparator()
        for option in self._reasoning_options:
            option_id = str(option.get("id") or "").strip().lower()
            label = self._option_label(option)
            prefix = "\u2713 " if option_id == current else "   "
            action = menu.addAction(prefix + label)
            action.triggered.connect(
                lambda _checked=False, oid=option_id: self._set_reasoning_option(oid)
            )
        pos = self._reasoning_btn.mapToGlobal(
            QPoint(0, self._reasoning_btn.height() + 4)
        )
        menu.exec(pos)

    def _set_reasoning_option(self, option_id: str) -> None:
        option_ids = {
            str(opt.get("id") or "").strip().lower()
            for opt in self._reasoning_options
        }
        option_id = str(option_id or "auto").strip().lower()
        if option_id not in option_ids:
            option_id = "auto"
        cfg = get_reasoning_settings()
        models = dict((cfg or {}).get("models") or {})
        model_key = (
            f"chatgpt_account:{self._mid}"
            if self._provider == "chatgpt_account" else self._mid
        )
        models[model_key] = option_id
        cfg["models"] = models
        set_reasoning_settings(cfg)
        self._refresh_reasoning_chip()

    def _retranslate_reasoning(self) -> None:
        if self._reasoning_btn is not None:
            self._reasoning_btn.setToolTip(i18n.t("reasoning.tooltip"))
        self._refresh_reasoning_chip()


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


class _FocusTooltipButton(QPushButton):
    """Compact info control whose tooltip is also reachable by keyboard."""

    def focusInEvent(self, event):
        super().focusInEvent(event)
        QToolTip.showText(
            self.mapToGlobal(self.rect().bottomLeft()), self.toolTip(), self
        )

    def focusOutEvent(self, event):
        QToolTip.hideText()
        super().focusOutEvent(event)


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
        lay.setContentsMargins(11, 11, 11, 11)
        lay.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(8)
        # Single subtle violet dot for every provider (no per-provider colour).
        dot = QLabel()
        dot.setObjectName("themeDot")
        dot.setFixedSize(8, 8)
        name = QLabel(
            i18n.t("chatgpt_account.title")
            if provider == "chatgpt_account"
            else PROVIDERS.get(provider, {}).get("name") or provider.title()
        )
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

    model_changed = pyqtSignal(str, str)
    account_refresh_requested = pyqtSignal()
    local_models_refresh_requested = pyqtSignal()

    PROVIDER_ORDER = (
        "chatgpt_account", "anthropic", "openai", "deepseek", "google",
        "xai", "groq", "mistral", "ollama",
    )

    def __init__(self):
        super().__init__()
        self._current_model: str | None = None
        self._current_provider: str | None = None
        self._remote: list = []
        self._account_status: dict | None = None
        self._inventory: list[dict] = []
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
        scroll.setVerticalScrollBarPolicy(
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

        self._account_refresh_timer = QTimer(self)
        self._account_refresh_timer.setInterval(5000)
        self._account_refresh_timer.timeout.connect(
            self.account_refresh_requested.emit
        )

    def _retranslate(self) -> None:
        self._back_btn.setText("←  " + i18n.t("models.back"))
        self.refresh()

    def set_remote_models(self, remote: list) -> None:
        self._remote = remote or []
        self.set_inventory(build_model_inventory(
            list(get_all_models(remote_models=self._remote)),
            configured_api_providers(load_api_keys()),
            self._account_status,
        ))

    def set_current(self, model_id: str | None, provider: str | None = None) -> None:
        self._current_model = model_id
        self._current_provider = provider
        self.refresh()

    def set_account_status(self, status: dict) -> None:
        self._account_status = dict(status or {})
        self.set_inventory(build_model_inventory(
            list(get_all_models(remote_models=self._remote)),
            configured_api_providers(load_api_keys()),
            self._account_status,
        ))

    def set_inventory(self, inventory: list[dict]) -> None:
        """Consume the MainPopup-owned normalized cached inventory."""
        self._inventory = [
            dict(entry) for entry in inventory if isinstance(entry, dict)
        ]
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(50, self._request_visible_refreshes)
        self._account_refresh_timer.start()

    def _request_visible_refreshes(self) -> None:
        if not self.isVisible():
            return
        self.account_refresh_requested.emit()
        self.local_models_refresh_requested.emit()

    def hideEvent(self, event):
        self._account_refresh_timer.stop()
        super().hideEvent(event)

    def _account_models(self) -> list[dict]:
        return [
            self._row_model(entry) for entry in self._inventory
            if entry.get("provider_id") == "chatgpt_account"
        ]

    @staticmethod
    def _row_model(entry: dict) -> dict:
        return {
            "id": entry.get("model_id") or "",
            "provider": entry.get("provider_id") or "",
            "label": entry.get("model_display_name") or entry.get("model_id") or "?",
            "reasoning_options": list(entry.get("reasoning_options") or []),
        }

    def _account_is_visible(self, models: list[dict] | None = None) -> bool:
        usable_models = self._account_models() if models is None else models
        return bool(usable_models)

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
                w.hide()
                w.deleteLater()
        if self._view and not any(
            entry.get("provider_id") == self._view for entry in self._inventory
        ):
            self._view = None
        if self._view is None:
            self._build_grid()
        else:
            self._build_model_list(self._view)

    # ── Level 1: provider grid ──────────────────────────────────────────────

    def _build_grid(self) -> None:
        self._back_btn.hide()
        self._title.setText(i18n.t("models.title"))
        self._sub.setText(i18n.t("models.choose_provider_sub"))

        counts: dict[str, int] = {}
        for entry in self._inventory:
            provider_id = str(entry.get("provider_id") or "")
            if provider_id:
                counts[provider_id] = counts.get(provider_id, 0) + 1
        configured = [
            p for p in self.PROVIDER_ORDER
            if counts.get(p)
        ]

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
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
            i18n.t("chatgpt_account.title")
            if provider == "chatgpt_account"
            else PROVIDERS.get(provider, {}).get("name") or provider.title()
        )
        self._sub.setText(i18n.t("models.pick_model_sub"))

        models = [
            self._row_model(entry) for entry in self._inventory
            if entry.get("provider_id") == provider
        ]
        models.sort(key=lambda m: m.get("label", ""))

        if not models:
            empty = QLabel(i18n.t("models.none_for_provider"))
            empty.setObjectName("panelEmpty")
            self._host_layout.insertWidget(0, empty)
            return

        idx = 0
        for m in models:
            is_account = provider == "chatgpt_account"
            row = _ModelRow(
                m,
                is_active=(
                    m["id"] == self._current_model
                    and provider == self._current_provider
                ),
                reasoning_options=m.get("reasoning_options") if is_account else None,
            )
            if is_account:
                row.selected.connect(self._select_account_model)
            else:
                row.selected.connect(
                    lambda model_id, p=provider: self.model_changed.emit(model_id, p)
                )
            self._host_layout.insertWidget(idx, row)
            idx += 1

    def _select_account_model(self, model_id: str) -> None:
        try:
            from osia.chatgpt_account.provider import get_account_provider
            get_account_provider().set_selected_model(str(model_id))
        except Exception:
            return
        if self._account_status is not None:
            self._account_status["selectedModel"] = str(model_id)
        self.model_changed.emit(str(model_id), "chatgpt_account")
        self.refresh()


# ── Settings panel ──────────────────────────────────────────────────────────

class _SettingsNavigationList(QListWidget):
    """Category list whose wheel events stay inside the Settings navigation."""

    def __init__(self):
        super().__init__()
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt method name)
        pixel_y = event.pixelDelta().y()
        if pixel_y:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() - pixel_y)
        else:
            super().wheelEvent(event)
        # QAbstractItemView ignores wheel events at a scroll boundary.  Accept
        # them here so Qt never propagates them into the sibling detail pane.
        event.accept()

    def scrollTo(self, index, hint=None) -> None:  # noqa: N802 (Qt method name)
        """Keep category selection from moving the independently scrolled list."""
        del index, hint


class _SettingsDetailStack(QStackedWidget):
    """Stack used inside the Settings detail scroll area.

    A regular QStackedWidget reports a size hint/minimum from the largest page,
    including hidden pages. In a scroll area's resizable widget this can make one
    wide/tall Settings category force every other category to an oversized child
    geometry; with scrollbars hidden, the viewport clips the content. The detail
    pane only needs the current page's hint, while its minimum must stay soft so
    the scroll area can fit the page to the existing popup width.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt method name)
        current = self.currentWidget()
        if current is not None:
            return current.sizeHint()
        return super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt method name)
        return QSize(0, 0)

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


class _PairingQrDialog(_PopoverDialog):
    """In-memory QR view for exactly one currently active pairing token."""

    def __init__(self, parent, token: str, anchor=None):
        super().__init__(parent, anchor=anchor)
        self.setObjectName("pairingQrDialog")
        self._token = token
        self._active = True

        title = QLabel(i18n.t("settings.qr_title"))
        title.setObjectName("dialogTitle")
        self._card_layout.addWidget(title)

        help_label = QLabel(i18n.t("settings.qr_help"))
        help_label.setObjectName("sectionDesc")
        help_label.setWordWrap(True)
        help_label.setMaximumWidth(300)
        self._card_layout.addWidget(help_label)

        self._qr_label = QLabel()
        self._qr_label.setObjectName("pairingQrImage")
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(268, 268)
        self._qr_label.setStyleSheet(
            "QLabel#pairingQrImage { background: white; border-radius: 10px; "
            "padding: 8px; color: #1a1a1a; }"
        )
        self._qr_label.setPixmap(_pairing_qr_pixmap(token))
        self._card_layout.addWidget(
            self._qr_label, alignment=Qt.AlignmentFlag.AlignHCenter
        )

        close_btn = QPushButton(i18n.t("settings.close"))
        close_btn.setObjectName("primaryBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        self._card_layout.addWidget(
            close_btn, alignment=Qt.AlignmentFlag.AlignRight
        )

        self._token_watch = QTimer(self)
        self._token_watch.setInterval(500)
        self._token_watch.timeout.connect(self._invalidate_if_rotated)
        self._token_watch.start()

    def _invalidate_if_rotated(self) -> None:
        if not self._active or load_mobile_token() == self._token:
            return
        self._active = False
        self._token = ""
        self._token_watch.stop()
        self._qr_label.clear()
        self._qr_label.setText(i18n.t("settings.qr_inactive"))
        self._qr_label.setWordWrap(True)

    def done(self, result: int) -> None:
        self._token_watch.stop()
        self._token = ""
        self._qr_label.clear()
        super().done(result)


class _MemoryEditDialog(_PopoverDialog):
    """Small settings popover for editing one local memory entry."""

    _TYPES = ("route", "preference", "fact", "workflow", "contact", "shortcut")
    _CATEGORIES = (
        "app", "site", "deep_link", "folder", "file", "game",
        "user", "workflow", "contact", "other",
    )
    _KINDS = ("path", "url", "uri", "steam_appid", "text", "steps")

    def __init__(self, parent, entry: dict, anchor=None):
        super().__init__(parent, anchor=anchor)
        self.setObjectName("memoryEditDialog")
        try:
            self._entry = json.loads(json.dumps(entry, ensure_ascii=False))
        except Exception:
            self._entry = dict(entry or {})
        self.saved_entry = None

        lay = self._card_layout
        title = QLabel(i18n.t("settings.memory_edit_title"))
        title.setObjectName("dialogTitle")
        lay.addWidget(title)

        self._type_combo = self._combo(self._TYPES, self._entry.get("type"))
        self._category_combo = self._combo(
            self._CATEGORIES, self._entry.get("category")
        )
        value = self._entry.get("value") if isinstance(self._entry.get("value"), dict) else {}
        self._kind_combo = self._combo(self._KINDS, value.get("kind"))

        self._title_input = self._line(
            "settings.memory_field_title", self._entry.get("title", "")
        )
        self._key_input = self._line(
            "settings.memory_field_key", self._entry.get("key", "")
        )
        self._labeled_widget("settings.memory_field_type", self._type_combo)
        self._labeled_widget("settings.memory_field_category", self._category_combo)
        self._labeled_widget("settings.memory_field_value_kind", self._kind_combo)

        self._value_edit = QTextEdit()
        self._value_edit.setObjectName("mdEditor")
        self._value_edit.setMinimumSize(320, 120)
        data = value.get("data", "")
        if isinstance(data, (dict, list)):
            self._value_edit.setPlainText(
                json.dumps(data, ensure_ascii=False, indent=2)
            )
        else:
            self._value_edit.setPlainText(str(data or ""))
        self._labeled_widget("settings.memory_field_value", self._value_edit)

        self._error = QLabel("")
        self._error.setObjectName("providerSub")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(
            f"color: {get_theme_manager().palette.error};"
        )
        lay.addWidget(self._error)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(i18n.t("settings.close"))
        cancel.setObjectName("ghostBtn")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        save = QPushButton(i18n.t("provider.save"))
        save.setObjectName("primaryBtn")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._on_save)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)

    def _combo(self, values: tuple[str, ...], current) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName("voiceCombo")
        for value in values:
            combo.addItem(value.replace("_", " "), value)
        selected = str(current or "").strip().lower()
        for idx, value in enumerate(values):
            if value == selected:
                combo.setCurrentIndex(idx)
                break
        return combo

    def _line(self, label_key: str, value) -> QLineEdit:
        line = QLineEdit(str(value or ""))
        line.setObjectName("keyInput")
        line.setMinimumWidth(320)
        self._labeled_widget(label_key, line)
        return line

    def _labeled_widget(self, label_key: str, widget: QWidget) -> None:
        label = QLabel(i18n.t(label_key))
        label.setObjectName("providerSub")
        self._card_layout.addWidget(label)
        self._card_layout.addWidget(widget)

    def _edited_value_data(self):
        raw = self._value_edit.toPlainText().strip()
        kind = self._kind_combo.currentData() or "text"
        if kind == "steps":
            if raw.startswith("[") or raw.startswith("{"):
                return json.loads(raw)
            return [line.strip() for line in raw.splitlines() if line.strip()]
        return raw

    def _on_save(self) -> None:
        try:
            updated = json.loads(json.dumps(self._entry, ensure_ascii=False))
            updated["type"] = self._type_combo.currentData() or "fact"
            updated["category"] = self._category_combo.currentData() or "other"
            updated["key"] = self._key_input.text().strip()
            updated["title"] = self._title_input.text().strip()
            updated["value"] = {
                "kind": self._kind_combo.currentData() or "text",
                "data": self._edited_value_data(),
            }
            saved = MemoryStore().upsert_entry(updated)
            if not MemoryStore().get_entry(saved["id"]):
                raise RuntimeError("memory entry was not readable after save")
            self.saved_entry = saved
            self.accept()
        except Exception as exc:
            self._error.setText(
                i18n.t("settings.memory_save_failed", error=f": {exc}")
            )


class _MemoryConfirmDialog(_PopoverDialog):
    """Small confirmation popover for destructive memory actions."""

    def __init__(self, parent, title_key: str, body_key: str, action_key: str, anchor=None):
        super().__init__(parent, anchor=anchor)
        title = QLabel(i18n.t(title_key))
        title.setObjectName("dialogTitle")
        self._card_layout.addWidget(title)

        body = QLabel(i18n.t(body_key))
        body.setObjectName("sectionSub")
        body.setWordWrap(True)
        body.setMaximumWidth(340)
        self._card_layout.addWidget(body)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(i18n.t("settings.close"))
        cancel.setObjectName("ghostBtn")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        action = QPushButton(i18n.t(action_key))
        action.setObjectName("memoryDangerAction")
        action.setCursor(Qt.CursorShape.PointingHandCursor)
        action.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(action)
        self._card_layout.addLayout(row)


class _MemoryClearDialog(_MemoryConfirmDialog):
    """Confirmation popover for clearing local OSIA memory."""

    def __init__(self, parent, anchor=None):
        super().__init__(
            parent,
            "settings.memory_clear_title",
            "settings.memory_clear_confirm",
            "settings.memory_clear_all",
            anchor=anchor,
        )


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
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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


class _McpCustomDialog(_PopoverDialog):
    """Add a custom MCP server (WOR-44 Phase 3).

    Captures a name, a transport (a stdio *command* — split on spaces into
    command + args — OR an *https URL*), an optional token and (for stdio) the
    env-var the token should be injected into. Validates locally and exposes the
    NON-secret server config in ``self.result_cfg`` plus ``self.result_token``;
    the caller persists the config to ``mcp_servers`` and the token to the
    gitignored store. It never connects — that is an explicit, separate action.
    """

    def __init__(self, parent, anchor=None):
        super().__init__(parent, anchor=anchor)
        self.result_cfg = None
        self.result_token = ""

        title = QLabel(i18n.t("mcp.custom_title"))
        title.setObjectName("dialogTitle")
        self._card_layout.addWidget(title)

        hint = QLabel(i18n.t("mcp.custom_hint"))
        hint.setObjectName("providerSub")
        hint.setWordWrap(True)
        self._card_layout.addWidget(hint)

        def _field(label_key, placeholder_key="", password=False):
            lab = QLabel(i18n.t(label_key))
            lab.setObjectName("providerSub")
            self._card_layout.addWidget(lab)
            le = QLineEdit()
            le.setObjectName("keyInput")
            le.setMinimumWidth(320)
            if placeholder_key:
                le.setPlaceholderText(i18n.t(placeholder_key))
            if password:
                le.setEchoMode(QLineEdit.EchoMode.Password)
            self._card_layout.addWidget(le)
            return le

        self._name = _field("mcp.field_name", "mcp.field_name_ph")
        self._command = _field("mcp.field_command", "mcp.field_command_ph")
        self._url = _field("mcp.field_url", "mcp.field_url_ph")
        self._token = _field("mcp.field_token_optional", "mcp.field_token_ph", password=True)
        self._token_env = _field("mcp.field_token_env_optional", "mcp.field_token_env_ph")

        self._error = QLabel("")
        self._error.setObjectName("providerSub")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(
            f"color: {get_theme_manager().palette.error};"
        )
        self._card_layout.addWidget(self._error)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton(i18n.t("settings.close"))
        cancel.setObjectName("ghostBtn")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ok = QPushButton(i18n.t("mcp.add_custom"))
        ok.setObjectName("primaryBtn")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.clicked.connect(self._on_ok)
        row.addWidget(cancel)
        row.addWidget(ok)
        self._card_layout.addLayout(row)

    def _on_ok(self) -> None:
        name = self._name.text().strip()
        command = self._command.text().strip()
        url = self._url.text().strip()
        token = self._token.text().strip()
        token_env = self._token_env.text().strip()

        if not name:
            self._error.setText(i18n.t("mcp.err_name"))
            return
        if bool(command) == bool(url):
            # need exactly one of command / url
            self._error.setText(i18n.t("mcp.err_transport"))
            return
        # Derive a stable id from the name (the namespace forbids "__").
        sid = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-").lower() or "custom"

        if url:
            if not (url.startswith("https://") or url.startswith("http://")):
                self._error.setText(i18n.t("mcp.err_url"))
                return
            # The manager only speaks stdio so far — a remote (HTTP) server
            # could be saved but never connected, a silent dead-end. Refuse it
            # (and don't store its token) until HTTP transport lands (Phase 5).
            self._error.setText(i18n.t("mcp.err_http_soon"))
            return
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        if not parts:
            self._error.setText(i18n.t("mcp.err_transport"))
            return
        cfg = {
            "id": sid, "name": name, "transport": "stdio",
            "command": parts[0], "args": parts[1:], "url": None, "env": {},
            "auth_type": "token" if token else "none", "enabled": False,
            "token_env": token_env or None, "custom": True,
        }
        self.result_cfg = cfg
        self.result_token = token
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
    # Emitted when the "Enable wake word" toggle changes. The host
    # (osia.main.wake_mode) connects it to the wake-word detector start/stop.
    wake_word_enabled_changed = pyqtSignal(bool)
    # Carries startup-service probe results [(unit, state, enabled), ...] from
    # the background probe thread back onto the GUI thread (WOR perf fix — the
    # probe does slow tasklist/socket calls that must not block the UI).
    _startup_status_ready = pyqtSignal(list)
    # Carries token-safe native Gmail REST status from a background probe.
    _gmail_rest_status_ready = pyqtSignal(dict)
    # Managed ChatGPT account operations always run off the GUI thread.
    _chatgpt_status_ready = pyqtSignal(dict)
    _chatgpt_login_ready = pyqtSignal(dict)
    chatgpt_status_changed = pyqtSignal(dict)
    local_models_refresh_requested = pyqtSignal()
    getting_started_requested = pyqtSignal()
    permissions_changed = pyqtSignal(dict)
    _shutdown_finished = pyqtSignal(bool)

    # Order AI provider cards appear in Section 1 (Ollama is keyless and shown
    # last as an info-only row).
    _AI_PROVIDER_ORDER = ("anthropic", "openai", "google", "deepseek", "xai", "groq", "mistral")
    _SETTINGS_STATE_TTL = timedelta(seconds=5)
    _CHATGPT_STATUS_TTL = timedelta(seconds=15)
    _STARTUP_STATUS_TTL = timedelta(seconds=5)

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
    _LIVEAPI_AVAILABLE = False
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

    def __init__(self, update_controller: UpdateController | None = None):
        super().__init__()

        self._theme = get_theme_manager()
        self._motion = MotionController(
            self,
            reduced_motion=lambda: (
                reduced_motion_requested(self) or not self.isVisible()
            ),
        )
        self._provider_cards: dict[str, _ProviderCard] = {}
        self._tts_cards: dict[str, _ProviderCard] = {}
        self._liveapi_cards: dict[str, _ProviderCard] = {}
        self._remote_models: list = []
        self._mobile_token: str = ""
        self._startup_refresh_guard = False
        # True while a background startup-status probe is in flight, so the 5s
        # timer / repeated shows never pile up overlapping probe threads.
        self._startup_probe_busy = False
        self._startup_refresh_pending = False
        self._startup_last_probe = datetime.min
        self._startup_status_ready.connect(self._apply_startup_status)
        self._shutdown_finished.connect(self._on_shutdown_finished)
        self._gmail_rest_status = {
            "status": "disconnected",
            "connected": False,
            "error": "",
            "emailAddress": None,
            "capabilities": {
                "readOnly": False,
                "compose": False,
                "modify": False,
                "composeUpgradeRequired": False,
                "modifyUpgradeRequired": False,
            },
        }
        self._gmail_rest_probe_busy = False
        self._gmail_rest_last_probe = datetime.min
        self._gmail_rest_oauth_busy = False
        self._gmail_rest_credentials_editing = False
        self._gmail_rest_status_ready.connect(self._apply_gmail_rest_status)
        self._chatgpt_probe_busy = False
        self._chatgpt_action_busy = False
        self._chatgpt_last_status = None
        self._chatgpt_last_probe = datetime.min
        self._chatgpt_request_generation = 0
        self._chatgpt_active_probe_generation: int | None = None
        self._chatgpt_refresh_pending = False
        self._chatgpt_retry_view_state: tuple[int, int] | None = None
        self._chatgpt_status_ready.connect(self._apply_chatgpt_status)
        self._chatgpt_login_ready.connect(self._apply_chatgpt_login_action)
        self._chatgpt_file_watcher = QFileSystemWatcher(self)
        self._chatgpt_file_watcher.fileChanged.connect(
            self._on_chatgpt_auth_storage_changed
        )
        self._chatgpt_file_watcher.directoryChanged.connect(
            self._on_chatgpt_auth_storage_changed
        )
        self._configure_chatgpt_file_watcher()
        self._startup_rows: list[tuple[str, QLabel, OsiaSwitch]] = []
        # Guards the autostart toggle against re-entrant stateChanged while it is
        # being reverted (e.g. on an unsupported platform). See WOR-22.
        self._autostart_guard = False

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

        # WOR-48 P2 — master-detail layout. A category list on the LEFT drives a
        # QStackedWidget on the RIGHT; each stack page groups several of the
        # existing section frames. The section frames are built EXACTLY as before
        # (same builders, same call order) and only reparented into pages — which
        # is invisible to the handlers / _load_state / _retranslate_sections, all
        # of which address widgets via the self._xxx attributes the builders set.
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)

        # LEFT — category navigation.
        self._settings_nav = _SettingsNavigationList()
        self._settings_nav.setObjectName("settingsNav")
        self._settings_nav.setFixedWidth(110)
        self._settings_nav.setWordWrap(True)
        self._settings_nav.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._settings_nav.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        body.addWidget(self._settings_nav)

        # RIGHT — keep a QScrollArea#panelScroll (so the transparent-bg QSS and
        # ScrollBarAlwaysOff still apply) wrapping a QStackedWidget of pages.
        self._settings_scroll = QScrollArea()
        self._settings_scroll.setObjectName("panelScroll")
        self._settings_scroll.setWidgetResizable(True)
        self._settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._settings_stack = _SettingsDetailStack()
        self._settings_stack.setObjectName("settingsStack")

        # Build every section ONCE, in the original column order, so any
        # construction-time ordering assumptions are preserved unchanged.
        sec_language  = self._build_language_section()
        sec_theme     = self._build_theme_section()
        sec_capsule   = self._build_capsule_section()       # Capsule + autostart
        sec_restart   = self._build_restart_section()       # Controlled relaunch
        sec_guide     = self._build_getting_started_section()
        sec_permissions = self._build_permissions_section()
        sec_ai_prov   = self._build_ai_providers_section()  # Section 1
        sec_voice     = self._build_voice_section()         # Section 2 (TTS)
        sec_wake      = self._build_wake_section()           # Wake-word greeting
        sec_stt       = self._build_stt_section()           # Section 3 (STT)
        sec_liveapi   = self._build_liveapi_section()       # Section 4 (Live API)
        sec_hierarchy = self._build_ai_hierarchy_section()  # AI Task Hierarchy
        sec_cron      = self._build_cron_section()          # Scheduled Tasks
        sec_heartbeat = self._build_heartbeat_section()     # Heartbeat
        sec_apikeys   = self._build_apikeys_section()       # API Key Fallback
        sec_mobile    = self._build_mobile_section()        # Mobile Connection
        sec_startup   = self._build_startup_section()       # Autostart services
        sec_memory    = self._build_memory_section()        # Local Memory
        sec_mcp       = self._build_mcp_section()           # MCP / Connectors (WOR-44)
        sec_legal     = self._build_legal_section()         # Legal versions + links
        sec_updates   = (
            UpdateSettingsSection(update_controller)
            if update_controller is not None else None
        )

        # (nav i18n key, [sections on that page]) — 9 categories, all 17 frames,
        # each frame used exactly once. MCP/Connectors gets its OWN category (not
        # folded into Advanced) — it is a central, growing capability that
        # warrants top-level discoverability.
        self._settings_pages = [
            ("settings.cat.general",  [sec_language, sec_theme, sec_capsule, sec_restart, sec_guide]),
            ("settings.cat.permissions", [sec_permissions]),
            ("settings.cat.models",   [sec_ai_prov, sec_hierarchy, sec_apikeys]),
            ("settings.cat.voice",    [sec_voice, sec_stt, sec_liveapi]),
            ("settings.cat.wake",     [sec_wake]),
            ("settings.cat.memory",   [sec_memory]),
            ("settings.cat.mcp",      [sec_mcp]),
            ("settings.cat.mobile",   [sec_mobile, sec_startup]),
            ("settings.cat.advanced", [sec_cron, sec_heartbeat]),
            *(([("settings.cat.updates", [sec_updates])]) if sec_updates is not None else []),
            ("settings.cat.legal",    [sec_legal]),
        ]

        for _key, _sections in self._settings_pages:
            page = QWidget()
            # Keep each stack page transparent like the old panelHost so the
            # sections sit on the app's dark glass, not the grey palette.
            page.setObjectName("panelHost")
            pv = QVBoxLayout(page)
            pv.setContentsMargins(0, 0, 6, 0)
            pv.setSpacing(11)
            for _sec in _sections:
                pv.addWidget(_sec)
            pv.addStretch(1)
            self._settings_stack.addWidget(page)
            self._settings_nav.addItem(i18n.t(_key))

        self._settings_scroll.setWidget(self._settings_stack)
        body.addWidget(self._settings_scroll, 1)
        outer.addLayout(body, 1)

        # Wire the nav -> stack, default to the first category.
        self._settings_nav.currentRowChanged.connect(
            self._on_settings_category_changed
        )
        self._settings_nav.setCurrentRow(0)

        # Re-translate this panel's own section chrome on language change.
        i18n.add_listener(self._retranslate_sections)
        # WOR-48 P2 — keep the category-nav labels in sync with the language too
        # (its own small listener, so _retranslate_sections stays untouched).
        i18n.add_listener(self._retranslate_nav)
        self.destroyed.connect(
            lambda: (i18n.remove_listener(self._retranslate_sections),
                     i18n.remove_listener(self._retranslate_nav))
        )

        self._apply_style()
        self._state_loaded_at = datetime.min
        self._state_refresh_scheduled = False
        self._load_state()

        # WOR-48 P3 — cap every combo's minimum width so no page's minimum
        # exceeds the WIDE master-detail viewport (see _cap_combo_width).
        # Combos created later at runtime (_build_hier_row) apply it themselves.
        for _combo in self.findChildren(QComboBox):
            _cap_combo_width(_combo)
            self._guard_settings_wheel(_combo)

        # Refresh startup status while visible.
        self._startup_timer = QTimer(self)
        self._startup_timer.setInterval(5000)
        self._startup_timer.timeout.connect(self._refresh_startup_status)

        # WOR-44 — poll live MCP per-server status (a cheap in-memory snapshot
        # from the manager) while the settings panel is visible, so a pill flips
        # connecting→connected/failed without blocking the UI thread.
        self._mcp_timer = QTimer(self)
        self._mcp_timer.setInterval(1200)
        self._mcp_timer.timeout.connect(self._refresh_mcp_status)

    # ── Section builders ──────────────────────────────────────────────────

    def set_remote_models(self, remote: list) -> None:
        self._remote_models = remote or []
        if hasattr(self, "_cron_host"):
            self._render_cron()

    def _on_settings_category_changed(self, row: int) -> None:
        if row < 0:
            return
        self._settings_stack.setCurrentIndex(row)
        # WOR-198: reset only the independently owned right detail pane.
        self._settings_scroll.verticalScrollBar().setValue(0)
        page = self._settings_stack.currentWidget()
        if page is not None:
            self._motion.fade_in(page, MOTION.page_ms)
        try:
            key = self._settings_pages[row][0]
        except Exception:
            return
        if key == "settings.cat.memory":
            self._refresh_memory_entries()

    def show_category(self, category_key: str) -> None:
        for row, (key, _sections) in enumerate(self._settings_pages):
            if key == category_key:
                self._settings_nav.setCurrentRow(row)
                self._settings_scroll.verticalScrollBar().setValue(0)
                return

    def show_updates(self) -> None:
        self.show_category("settings.cat.updates")

    def show_answer_settings(self, *, chatgpt: bool = False) -> None:
        self.show_category("settings.cat.models")
        if chatgpt:
            index = self._ai_provider_combo.findData("chatgpt_account")
            if index >= 0:
                self._ai_provider_combo.setCurrentIndex(index)

    def show_mobile_settings(self) -> None:
        self.show_category("settings.cat.mobile")

    def _guard_settings_wheel(self, combo: QComboBox) -> None:
        """Keep a closed Settings combo from consuming the page wheel."""
        combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        combo.installEventFilter(self)

    def _scroll_settings_from_wheel(self, event) -> bool:
        """Apply one wheel event to the Settings detail scrollbar."""
        bar = self._settings_scroll.verticalScrollBar()
        pixel_y = event.pixelDelta().y()
        angle_y = event.angleDelta().y()
        if pixel_y:
            delta = pixel_y
        elif angle_y:
            steps = angle_y / 120.0
            delta = round(steps * max(24, bar.singleStep() * 3))
        else:
            return False
        bar.setValue(bar.value() - delta)
        event.accept()
        return True

    def eventFilter(self, watched, event):  # noqa: N802 (Qt method name)
        if event.type() == QEvent.Type.Wheel:
            if isinstance(watched, QComboBox):
                # An explicitly opened popup remains a normal Qt item view.
                if watched.view().isVisible():
                    return False
                return self._scroll_settings_from_wheel(event)
        return super().eventFilter(watched, event)

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
        self._language_desc = QLabel(i18n.t("settings.language_desc"))
        self._language_desc.setObjectName("sectionDesc")
        self._language_desc.setWordWrap(True)
        l.addWidget(self._language_desc)

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

    def _build_theme_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self._theme_title = QLabel(i18n.t("theme.section"))
        self._theme_title.setObjectName("sectionTitle")
        layout.addWidget(self._theme_title)
        self._theme_sub = QLabel(i18n.t("theme.section_sub"))
        self._theme_sub.setObjectName("sectionSub")
        self._theme_sub.setWordWrap(True)
        layout.addWidget(self._theme_sub)
        self._theme_desc = QLabel(i18n.t("theme.section_desc"))
        self._theme_desc.setObjectName("sectionDesc")
        self._theme_desc.setWordWrap(True)
        layout.addWidget(self._theme_desc)

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_buttons: dict[str, QPushButton] = {}
        for index, palette in enumerate(PALETTES.values()):
            button = QPushButton(i18n.t(palette.name_key))
            button.setObjectName("themeChoice")
            button.setCheckable(True)
            button.setProperty("themeKey", palette.key)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            preview = QPixmap(36, 12)
            preview.fill(Qt.GlobalColor.transparent)
            painter = QPainter(preview)
            painter.fillRect(0, 0, 12, 12, palette.color("background"))
            painter.fillRect(12, 0, 12, 12, palette.color("surface"))
            painter.fillRect(24, 0, 12, 12, palette.color("primary"))
            painter.end()
            button.setIcon(QIcon(preview))
            button.setIconSize(QSize(36, 12))
            button.clicked.connect(
                lambda checked=False, key=palette.key:
                self._on_theme_selected(key) if checked else None
            )
            self._theme_group.addButton(button)
            self._theme_buttons[palette.key] = button
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        self._sync_theme_buttons()
        self._theme.theme_changed.connect(self._on_theme_changed)
        return wrap

    def _on_theme_selected(self, key: str) -> None:
        self._theme.set_theme(key)

    def _on_theme_changed(self, _key: str) -> None:
        self._sync_theme_buttons()
        for pill in getattr(self, "_mcp_pills", {}).values():
            state = getattr(pill, "_mcp_state", "disconnected")
            pill._mcp_state = None
            self._set_mcp_pill(pill, state)
        if hasattr(self, "_theme_title"):
            self._motion.fade_in(self._theme_title, MOTION.state_ms)

    def _sync_theme_buttons(self) -> None:
        for key, button in getattr(self, "_theme_buttons", {}).items():
            blocked = button.blockSignals(True)
            button.setChecked(key == self._theme.key)
            button.blockSignals(blocked)

    def _retranslate_language_section(self) -> None:
        self._language_title.setText(i18n.t("settings.language"))
        self._language_sub.setText(i18n.t("settings.language_sub"))
        self._language_desc.setText(i18n.t("settings.language_desc"))
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
        self._capsule_sub = QLabel(i18n.t("settings.capsule_sub"))
        self._capsule_sub.setObjectName("sectionSub")
        self._capsule_sub.setWordWrap(True)
        self._capsule_desc = QLabel(i18n.t("settings.capsule_desc"))
        self._capsule_desc.setObjectName("sectionDesc")
        self._capsule_desc.setWordWrap(True)
        l.addWidget(self._capsule_title)
        l.addWidget(self._capsule_sub)
        l.addWidget(self._capsule_desc)

        # Toggle: show / hide the floating capsule window.
        show_row = QHBoxLayout()
        self._show_capsule_label = QLabel(i18n.t("settings.show_capsule"))
        self._show_capsule_label.setObjectName("providerName")
        self._show_capsule_label.setWordWrap(True)
        self._show_capsule_toggle = OsiaSwitch()
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
        self._autostart_toggle = OsiaSwitch()
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

    def _build_restart_section(self) -> QFrame:
        """Controlled desktop relaunch without changing background services."""
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self._restart_title = QLabel(i18n.t("settings.restart"))
        self._restart_title.setObjectName("sectionTitle")
        self._restart_title.setWordWrap(True)
        self._restart_sub = QLabel(i18n.t("settings.restart_sub"))
        self._restart_sub.setObjectName("sectionSub")
        self._restart_sub.setWordWrap(True)
        self._restart_desc = QLabel(i18n.t("settings.restart_desc"))
        self._restart_desc.setObjectName("sectionDesc")
        self._restart_desc.setWordWrap(True)
        layout.addWidget(self._restart_title)
        layout.addWidget(self._restart_sub)
        layout.addWidget(self._restart_desc)

        self._restart_status = QLabel()
        self._restart_status.setObjectName("sectionSub")
        self._restart_status.setWordWrap(True)
        self._restart_status.hide()
        layout.addWidget(self._restart_status)

        self._restart_btn = QPushButton(i18n.t("settings.restart_button"))
        self._restart_btn.setObjectName("primaryBtn")
        self._restart_btn.clicked.connect(self._on_restart_clicked)
        layout.addWidget(self._restart_btn)
        self._close_osia_btn = QPushButton(i18n.t("settings.close_button"))
        self._close_osia_btn.setObjectName("destructiveSecondaryBtn")
        self._close_osia_btn.setAccessibleName(i18n.t("settings.close_button"))
        self._close_osia_btn.clicked.connect(self._on_close_osia_clicked)
        layout.addWidget(self._close_osia_btn)
        self._restart_pending = False
        self._shutdown_pending = False
        self._power_status_key = ""
        return wrap

    def _on_restart_clicked(self) -> None:
        """Confirm once, prepare the coordinator, then quit Qt gracefully."""
        if self._restart_pending or self._shutdown_pending:
            return
        answer = QMessageBox.question(
            self,
            i18n.t("settings.restart_confirm_title"),
            i18n.t("settings.restart_confirm_text"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._restart_pending = True
        self._restart_btn.setEnabled(False)
        self._close_osia_btn.setEnabled(False)
        self._power_status_key = "settings.restart_pending"
        self._restart_status.setText(i18n.t(self._power_status_key))
        self._restart_status.show()
        try:
            from osia.relaunch import prepare_relaunch

            prepare_relaunch()
        except Exception:
            self._restart_pending = False
            self._restart_btn.setEnabled(True)
            self._close_osia_btn.setEnabled(True)
            self._power_status_key = "settings.restart_error"
            self._restart_status.setText(i18n.t(self._power_status_key))
            self._restart_status.show()
            return

        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _confirm_close_osia(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(i18n.t("settings.close_confirm_title"))
        box.setText(i18n.t("settings.close_confirm_text"))
        close_button = box.addButton(
            i18n.t("settings.close_button"), QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_button = box.addButton(
            i18n.t("settings.close_cancel"), QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec()
        return box.clickedButton() is close_button

    def _on_close_osia_clicked(self) -> None:
        """Stop the owned mobile service, then quit Qt without a relaunch."""
        if self._restart_pending or self._shutdown_pending:
            return
        if not self._confirm_close_osia():
            return
        self._shutdown_pending = True
        self._restart_btn.setEnabled(False)
        self._close_osia_btn.setEnabled(False)
        self._power_status_key = "settings.close_pending"
        self._restart_status.setText(i18n.t(self._power_status_key))
        self._restart_status.show()

        def _stop_owned_server() -> None:
            try:
                stopped = bool(stop_service(STARTUP_SERVICES[0]))
            except Exception:
                stopped = False
            self._shutdown_finished.emit(stopped)

        threading.Thread(
            target=_stop_owned_server,
            name="osia-session-shutdown",
            daemon=True,
        ).start()

    def _on_shutdown_finished(self, stopped: bool) -> None:
        if not self._shutdown_pending:
            return
        if not stopped:
            self._shutdown_pending = False
            self._restart_btn.setEnabled(True)
            self._close_osia_btn.setEnabled(True)
            self._power_status_key = "settings.close_error"
            self._restart_status.setText(i18n.t(self._power_status_key))
            self._restart_status.show()
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.setProperty("osiaIntentionalShutdown", True)
            except Exception:
                pass
            QTimer.singleShot(0, app.quit)

    def _build_getting_started_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        self._guide_title = QLabel(i18n.t("settings.getting_started"))
        self._guide_title.setObjectName("sectionTitle")
        self._guide_title.setWordWrap(True)
        self._guide_sub = QLabel(i18n.t("settings.getting_started_sub"))
        self._guide_sub.setObjectName("sectionSub")
        self._guide_sub.setWordWrap(True)
        self._guide_desc = QLabel(i18n.t("settings.getting_started_desc"))
        self._guide_desc.setObjectName("sectionDesc")
        self._guide_desc.setWordWrap(True)
        self._guide_btn = QPushButton(i18n.t("settings.getting_started_button"))
        self._guide_btn.setObjectName("ghostBtn")
        self._guide_btn.setAccessibleName(self._guide_btn.text())
        self._guide_btn.clicked.connect(self.getting_started_requested.emit)
        layout.addWidget(self._guide_title)
        layout.addWidget(self._guide_sub)
        layout.addWidget(self._guide_desc)
        layout.addWidget(self._guide_btn)
        return wrap

    def _build_permissions_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        self._permissions_title = QLabel(i18n.t("permissions.settings_title"))
        self._permissions_title.setObjectName("sectionTitle")
        self._permissions_title.setWordWrap(True)
        self._permissions_sub = QLabel(i18n.t("permissions.settings_sub"))
        self._permissions_sub.setObjectName("sectionSub")
        self._permissions_sub.setWordWrap(True)
        self._permissions_desc = QLabel(i18n.t("permissions.settings_desc"))
        self._permissions_desc.setObjectName("sectionDesc")
        self._permissions_desc.setWordWrap(True)
        from osia.gui.permissions_center import PermissionsPage

        self._permissions_page = PermissionsPage(self)
        self._permissions_page.state_changed.connect(self._on_permissions_changed)
        layout.addWidget(self._permissions_title)
        layout.addWidget(self._permissions_sub)
        layout.addWidget(self._permissions_desc)
        layout.addWidget(self._permissions_page)
        return wrap

    def _refresh_permissions_summary(self, *_args) -> None:
        self._permissions_page.refresh()

    def _on_permissions_changed(self, snapshot: dict) -> None:
        self._refresh_permissions_summary()
        self.permissions_changed.emit(snapshot)

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
        self._startup_desc = QLabel(i18n.t("settings.startup_desc"))
        self._startup_desc.setObjectName("sectionDesc")
        self._startup_desc.setWordWrap(True)
        l.addWidget(self._startup_desc)

        # (unit, i18n key) — the label widget + key are tracked in
        # self._startup_labels so they re-translate on language change.
        self._startup_labels: list[tuple[QLabel, str]] = []
        rows = [(STARTUP_SERVICES[0], "settings.startup_mobile_server")]
        for unit, label_key in rows:
            row = QHBoxLayout()
            label = QLabel(i18n.t(label_key))
            label.setObjectName("startupLabel")
            label.setWordWrap(True)
            self._startup_labels.append((label, label_key))
            dot = QLabel()
            dot.setObjectName("statusDot")
            dot.setWordWrap(True)
            dot.setFixedSize(10, 10)
            dot.setProperty("state", "stopped")
            toggle = OsiaSwitch()
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
        self._voice_desc = QLabel(i18n.t("settings.voice_providers_desc"))
        self._voice_desc.setObjectName("sectionDesc")
        self._voice_desc.setWordWrap(True)
        l.addWidget(self._voice_desc)

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
        self._wake_desc = QLabel(i18n.t("settings.wake_greeting_desc"))
        self._wake_desc.setObjectName("sectionDesc")
        self._wake_desc.setWordWrap(True)
        l.addWidget(self._wake_desc)

        # Compatibility row for the retired acoustic listener. It stays visibly
        # disabled and explains the supported Ctrl+Alt+P release path.
        ww_row = QHBoxLayout()
        self._wake_word_label = QLabel(i18n.t("settings.wake_word_enabled"))
        self._wake_word_label.setObjectName("providerName")
        self._wake_word_label.setWordWrap(True)
        self._wake_word_toggle = OsiaSwitch()
        self._wake_word_toggle.setEnabled(False)
        self._wake_word_toggle.setToolTip(
            i18n.t("settings.wake_word_enabled")
        )
        self._wake_word_toggle.stateChanged.connect(
            self._on_wake_word_enabled_changed
        )
        ww_row.addWidget(self._wake_word_label)
        ww_row.addStretch()
        ww_row.addWidget(self._wake_word_toggle)
        l.addLayout(ww_row)

        enable_row = QHBoxLayout()
        self._wake_enabled_label = QLabel(i18n.t("wake.greeting_enabled"))
        self._wake_enabled_label.setObjectName("providerName")
        self._wake_enabled_label.setWordWrap(True)
        self._wake_enabled_toggle = OsiaSwitch()
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

    def _on_wake_word_enabled_changed(self, _state) -> None:
        self._wake_word_toggle.blockSignals(True)
        self._wake_word_toggle.setChecked(False)
        self._wake_word_toggle.blockSignals(False)
        set_setting("wake_word_enabled", False)
        self.wake_word_enabled_changed.emit(False)

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
        self._stt_desc = QLabel(i18n.t("settings.whisper_providers_desc"))
        self._stt_desc.setObjectName("sectionDesc")
        self._stt_desc.setWordWrap(True)
        l.addWidget(self._stt_desc)

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
        self._liveapi_desc = QLabel(i18n.t("settings.liveapi_desc"))
        self._liveapi_desc.setObjectName("sectionDesc")
        self._liveapi_desc.setWordWrap(True)
        l.addWidget(self._liveapi_desc)

        # Master toggle — the configuration body below stays hidden until ON.
        enable_row = QHBoxLayout()
        self._liveapi_enabled_label = QLabel(i18n.t("liveapi.enabled"))
        self._liveapi_enabled_label.setObjectName("providerName")
        self._liveapi_enabled_label.setWordWrap(True)
        self._liveapi_soon = QLabel(i18n.t("liveapi.coming_soon"))
        self._liveapi_soon.setObjectName("comingSoonPill")
        self._liveapi_soon.setWordWrap(True)
        self._liveapi_enabled_toggle = OsiaSwitch()
        self._liveapi_enabled_toggle.setEnabled(self._LIVEAPI_AVAILABLE)
        self._liveapi_enabled_toggle.stateChanged.connect(
            self._on_liveapi_enabled_changed
        )
        enable_row.addWidget(self._liveapi_enabled_label)
        enable_row.addSpacing(8)
        enable_row.addWidget(self._liveapi_soon)
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
        self._liveapi_provider_combo.setEnabled(self._LIVEAPI_AVAILABLE)
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
        self._liveapi_model_combo.setEnabled(self._LIVEAPI_AVAILABLE)
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
        self._liveapi_docs_btn.setEnabled(self._LIVEAPI_AVAILABLE)
        self._liveapi_docs_btn.clicked.connect(self._on_liveapi_docs)
        docs_row.addWidget(self._liveapi_docs_btn)
        body.addLayout(docs_row)

        # API-key cards — one per provider, reusing the AI provider key store
        # (no duplicate storage). Only the active provider's card is shown.
        for key in self._LIVEAPI_PROVIDER_ORDER:
            card = _ProviderCard(self._LIVEAPI_KEY_PROVIDER[key])
            card.setEnabled(self._LIVEAPI_AVAILABLE)
            card.saved.connect(self._on_provider_saved)
            body.addWidget(card)
            self._liveapi_cards[key] = card

        # Violet info notice — STT (Whisper) + TTS are bypassed while active.
        self._liveapi_notice = QLabel(i18n.t("liveapi.notice"))
        self._liveapi_notice.setObjectName("liveApiNotice")
        self._liveapi_notice.setWordWrap(True)
        body.addWidget(self._liveapi_notice)

        self._liveapi_body.setEnabled(self._LIVEAPI_AVAILABLE)
        self._liveapi_sub.setEnabled(self._LIVEAPI_AVAILABLE)
        self._liveapi_desc.setEnabled(self._LIVEAPI_AVAILABLE)
        self._liveapi_enabled_label.setEnabled(self._LIVEAPI_AVAILABLE)
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
        self._liveapi_body.setVisible(
            self._LIVEAPI_AVAILABLE and self._liveapi_enabled_toggle.isChecked()
        )

    def _on_liveapi_enabled_changed(self, _state) -> None:
        if not self._LIVEAPI_AVAILABLE:
            self._liveapi_enabled_toggle.blockSignals(True)
            self._liveapi_enabled_toggle.setChecked(False)
            self._liveapi_enabled_toggle.blockSignals(False)
            self._apply_liveapi_visibility()
            return
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
        self._hier_desc = QLabel(i18n.t("settings.ai_hierarchy_desc"))
        self._hier_desc.setObjectName("sectionDesc")
        self._hier_desc.setWordWrap(True)
        l.addWidget(self._hier_desc)

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
        self._hier_enabled_toggle = OsiaSwitch()
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
        """Return ``[(display, "provider:model"), …]`` for usable providers."""
        keys = load_api_keys()
        choices = []
        models = list(get_all_models())
        account_status = self._chatgpt_last_status or {}
        if account_status.get("connected") and account_status.get("available"):
            for item in account_status.get("models") or []:
                if not isinstance(item, dict) or item.get("hidden"):
                    continue
                model_id = str(item.get("id") or item.get("model") or "").strip()
                modalities = item.get("inputModalities") or ["text", "image"]
                if not model_id or "text" not in modalities:
                    continue
                models.append({
                    "id": model_id,
                    "provider": "chatgpt_account",
                    "label": str(item.get("displayName") or model_id),
                })
        for m in models:
            provider = m["provider"]
            if (provider not in ("ollama", "chatgpt_account")
                    and not keys.get(provider)):
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
        _cap_combo_width(combo)   # WOR-48 P3 — created at runtime, cap it here
        self._guard_settings_wheel(combo)
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

    _CRON_MODES = ("minutes", "hours", "days", "daily", "days_at")
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
        self._cron_desc = QLabel(i18n.t("settings.cron_desc"))
        self._cron_desc.setObjectName("sectionDesc")
        self._cron_desc.setWordWrap(True)
        l.addWidget(self._cron_desc)

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
        self._cron_error = QLabel("")
        self._cron_error.setObjectName("providerSub")
        self._cron_error.setWordWrap(True)
        self._cron_error.hide()
        l.addWidget(self._cron_error)
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
        enable = OsiaSwitch()
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
        self._guard_settings_wheel(type_combo)
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
            self._guard_settings_wheel(mode_combo)
            for m in self._CRON_MODES:
                label = (
                    i18n.t(self._CRON_MODE_KEYS[m])
                    if m in self._CRON_MODE_KEYS
                    else f"{i18n.t('cron.every_days')} @"
                )
                mode_combo.addItem(label, m)
            for i in range(mode_combo.count()):
                if mode_combo.itemData(i) == mode:
                    mode_combo.setCurrentIndex(i)
                    break
            value_input = QLineEdit(value)
            value_input.setObjectName("keyInput")
            value_input.setFixedWidth(90)
            value_input.setPlaceholderText(
                self._cron_value_placeholder(mode))
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

        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        model_label = QLabel(i18n.t("settings.model"))
        model_label.setObjectName("fieldLabel")
        model_label.setMinimumWidth(64)
        model_combo = QComboBox()
        model_combo.setObjectName("voiceCombo")
        self._guard_settings_wheel(model_combo)
        _cap_combo_width(model_combo)
        self._populate_cron_model_combo(model_combo, job.get("model"))
        model_combo.currentIndexChanged.connect(
            lambda _i, idx=index, combo=model_combo:
                self._on_cron_model_changed(idx, combo)
        )
        model_row.addWidget(model_label)
        model_row.addWidget(model_combo, 1)
        v.addLayout(model_row)

        auth_status = str(job.get("authorization_status") or "authorization_required")
        revision = int(job.get("task_revision", 0) or 0)
        summary = ", ".join(
            str(item) for item in (job.get("capability_summary") or [])
        ) or "dynamic_scope_requires_review"
        bounds = job.get("authorization_bounds")
        if isinstance(bounds, dict) and bounds.get("mode"):
            bounds_text = str(bounds["mode"])
            if bounds.get("max_executions") is not None:
                bounds_text += f" ({int(bounds['max_executions'])} max)"
            summary = f"{summary}; {bounds_text}"
        expires = job.get("authorization_expires_at")
        expiration_text = i18n.t("cron.no_expiration")
        if expires is not None:
            try:
                expiration_text = datetime.fromtimestamp(
                    float(expires)
                ).astimezone().isoformat(timespec="minutes")
            except (TypeError, ValueError, OverflowError, OSError):
                expiration_text = str(expires)
        auth_text = i18n.t(
            "cron.authorization_line",
            status=auth_status,
            revision=revision,
            summary=summary,
            expiration=expiration_text,
        )
        auth_label = QLabel(auth_text)
        auth_label.setObjectName("providerSub")
        auth_label.setWordWrap(True)
        v.addWidget(auth_label)

        auth_buttons = QHBoxLayout()
        authorize = QPushButton(
            i18n.t(
                "cron.reauthorize" if auth_status == "authorized"
                else "cron.authorize"
            )
        )
        authorize.setObjectName("ghostBtn")
        authorize.clicked.connect(
            lambda _=False, i=index: self._on_cron_authorize(i)
        )
        revoke = QPushButton(i18n.t("cron.revoke_authorization"))
        revoke.setObjectName("ghostBtn")
        revoke.clicked.connect(lambda _=False, i=index: self._on_cron_revoke(i))
        disable = QPushButton(i18n.t("cron.disable"))
        disable.setObjectName("ghostBtn")
        disable.clicked.connect(lambda _=False, i=index: self._on_cron_disable(i))
        auth_buttons.addWidget(authorize)
        auth_buttons.addWidget(revoke)
        auth_buttons.addWidget(disable)
        auth_buttons.addStretch()
        v.addLayout(auth_buttons)
        return row

    def _cron_model_choices(self) -> list[tuple[str, dict]]:
        try:
            models = get_all_models(
                remote_models=getattr(self, "_remote_models", [])
            )
        except Exception:
            models = []
        try:
            keys = load_api_keys()
        except Exception:
            keys = {}
        out: list[tuple[str, dict]] = []
        seen: set[tuple[str, str]] = set()
        for m in models:
            if not isinstance(m, dict):
                continue
            model_id = str(m.get("id") or "").strip()
            provider = str(m.get("provider") or "").strip()
            if not model_id or not provider:
                continue
            if provider != "ollama" and not keys.get(provider):
                continue
            key = (provider, model_id)
            if key in seen:
                continue
            seen.add(key)
            model_label = str(m.get("label") or model_id).strip()
            provider_label = PROVIDERS.get(provider, {}).get("name", provider)
            out.append((
                f"{provider_label} · {model_label}",
                {"provider": provider, "id": model_id, "label": model_label},
            ))
        out.sort(key=lambda item: item[0].casefold())
        return out

    def _cron_model_metadata_from_id(
        self,
        model_id: str | None,
        *,
        provider: str | None = None,
        label: str | None = None,
    ) -> dict | None:
        model_id = str(model_id or "").strip()
        if not model_id:
            return None
        provider = str(provider or "").strip()
        for _display, meta in self._cron_model_choices():
            if meta.get("id") == model_id and (not provider or meta.get("provider") == provider):
                return dict(meta)
        if not provider:
            try:
                provider = provider_for_model_cached(model_id)
            except Exception:
                provider = ""
        return {
            "provider": provider or "ollama",
            "id": model_id,
            "label": str(label or model_id).strip() or model_id,
        }

    def _cron_current_model_metadata(self) -> dict | None:
        try:
            selected = (load_settings().get("selected_model") or "").strip()
        except Exception:
            selected = ""
        return self._cron_model_metadata_from_id(selected)

    def _populate_cron_model_combo(self, combo: QComboBox, model: object) -> None:
        combo.clear()
        combo.addItem(i18n.t("cron.model_default"), None)
        selected_idx = 0
        selected_id = ""
        selected_provider = ""
        if isinstance(model, dict):
            selected_id = str(model.get("id") or model.get("model") or "").strip()
            selected_provider = str(model.get("provider") or "").strip()
        choices = self._cron_model_choices()
        for label, meta in choices:
            combo.addItem(label, meta)
            if (
                selected_id
                and meta.get("id") == selected_id
                and (not selected_provider or meta.get("provider") == selected_provider)
            ):
                selected_idx = combo.count() - 1
        if selected_id and selected_idx == 0:
            meta = self._cron_model_metadata_from_id(
                selected_id,
                provider=selected_provider,
                label=str(model.get("label") or selected_id) if isinstance(model, dict) else selected_id,
            )
            provider_label = PROVIDERS.get(meta.get("provider"), {}).get(
                "name", meta.get("provider")
            )
            combo.addItem(f"{provider_label} · {meta.get('label')}", meta)
            selected_idx = combo.count() - 1
        combo.setCurrentIndex(selected_idx)

    def _on_cron_model_changed(self, index: int, combo: QComboBox) -> None:
        if not (0 <= index < len(self._cron_jobs)):
            return
        meta = combo.currentData()
        if isinstance(meta, dict):
            self._cron_jobs[index]["model"] = dict(meta)
        else:
            self._cron_jobs[index].pop("model", None)
        self._persist_cron()

    @staticmethod
    def _valid_cron_model(model: object) -> bool:
        if model in (None, ""):
            return True
        if not isinstance(model, dict):
            return False
        return bool(str(model.get("id") or model.get("model") or "").strip())

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
        if spec["kind"] == "day_interval_at":
            return (
                "days_at",
                f"{spec['days']} @ {timefmt.format_hm(spec['hour'], spec['minute'])}",
            )
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
    def _cron_value_placeholder(mode: str) -> str:
        if mode == "daily":
            return "2:30 PM" if timefmt.use_12h() else "14:30"
        if mode == "days_at":
            return "3 @ 2:30 PM" if timefmt.use_12h() else "3 @ 20:00"
        return "5"

    @classmethod
    def _compose_cron_schedule(cls, mode: str, value: str) -> str:
        """Build the canonical schedule string from the UI ``(mode, value)``."""
        value = (value or "").strip()
        if mode == "daily":
            return f"at {value}"
        if mode == "days_at":
            m = re.match(r"^\s*(\d+)\s*(?:@|at|a|Ã )?\s*(.+?)\s*$", value)
            if not m:
                return "every days at"
            hm = cls._parse_time_display(m.group(2))
            if hm is None:
                return "every days at"
            hour, minute = hm
            return f"every {int(m.group(1))} days at {hour:02d}:{minute:02d}"
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
        value_input.setPlaceholderText(self._cron_value_placeholder(mode))
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
        job = {"task": "", "schedule": "every 30 minutes", "enabled": False,
               "conversation_id": None}
        model_meta = self._cron_current_model_metadata()
        if model_meta:
            job["model"] = model_meta
        self._cron_jobs.append(
            job
        )
        self._persist_cron()
        self._render_cron()

    def _on_cron_remove(self, index: int) -> None:
        if 0 <= index < len(self._cron_jobs):
            self._cron_jobs.pop(index)
            self._persist_cron()
            self._render_cron()

    def _reload_cron_authorization_state(self) -> None:
        from osia import scheduled_tasks

        result = scheduled_tasks.list_scheduled_tasks()
        if result.get("ok"):
            self._cron_jobs = result.get("tasks", [])
        self._render_cron()

    def _on_cron_authorize(self, index: int) -> None:
        if not (0 <= index < len(self._cron_jobs)):
            return
        from osia import scheduled_tasks

        job = self._cron_jobs[index]
        detail = (
            f"{job.get('task', '')}\n\n"
            f"{', '.join(job.get('capability_summary') or [])}"
        )
        answer = QMessageBox.question(
            self,
            i18n.t("cron.authorize"),
            detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = scheduled_tasks.authorize_scheduled_task(
            str(job.get("id") or ""),
            origin_ref="desktop",
        )
        if not result.get("ok"):
            self._set_cron_error(
                str(result.get("message") or i18n.t("cron.authorization_required"))
            )
        else:
            self._set_cron_error(None)
        self._reload_cron_authorization_state()

    def _on_cron_revoke(self, index: int) -> None:
        if not (0 <= index < len(self._cron_jobs)):
            return
        from osia import scheduled_tasks

        scheduled_tasks.revoke_scheduled_task_authorization(
            str(self._cron_jobs[index].get("id") or "")
        )
        self._reload_cron_authorization_state()

    def _on_cron_disable(self, index: int) -> None:
        if not (0 <= index < len(self._cron_jobs)):
            return
        from osia import scheduled_tasks

        scheduled_tasks.disable_scheduled_task(
            str(self._cron_jobs[index].get("id") or "")
        )
        self._reload_cron_authorization_state()

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

    def _set_cron_error(self, message: str | None) -> None:
        label = getattr(self, "_cron_error", None)
        if label is None:
            return
        text = (message or "").strip()
        label.setText(text)
        label.setVisible(bool(text))

    def _persist_cron(self) -> None:
        from osia import scheduled_tasks

        clean = []
        for job in self._cron_jobs:
            if not self._valid_cron_model(job.get("model")):
                self._set_cron_error(i18n.t("cron.model_invalid"))
                return
            entry = dict(job)
            entry.update({
                "task": (job.get("task") or "").strip(),
                "schedule": (job.get("schedule") or "").strip(),
                "enabled": bool(job.get("enabled", False)),
                "conversation_id": job.get("conversation_id") or None,
            })
            clean.append(entry)
        result = scheduled_tasks.replace_scheduled_tasks_from_settings(
            clean,
            source_default="settings",
        )
        if result.get("ok"):
            self._cron_jobs = result.get("jobs", clean)
            self._set_cron_error(None)
        else:
            self._set_cron_error(
                result.get("message") or i18n.t("cron.model_invalid")
            )
            print(
                "[CRON] settings write rejected: "
                f"{result.get('code')}: {result.get('message')}"
            )

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
        self._hb_desc = QLabel(i18n.t("settings.heartbeat_desc"))
        self._hb_desc.setObjectName("sectionDesc")
        self._hb_desc.setWordWrap(True)
        l.addWidget(self._hb_desc)

        # Master toggle — the body below stays hidden until ON.
        enable_row = QHBoxLayout()
        self._hb_enabled_label = QLabel(i18n.t("heartbeat.enabled"))
        self._hb_enabled_label.setObjectName("providerName")
        self._hb_enabled_label.setWordWrap(True)
        self._hb_enabled_toggle = OsiaSwitch()
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
        self._hb_check_toggles: dict[str, OsiaSwitch] = {}
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
        toggle = OsiaSwitch()
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

    # Local Memory (WOR-89)

    def _build_memory_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._memory_title = QLabel(i18n.t("settings.memory"))
        self._memory_title.setObjectName("sectionTitle")
        self._memory_title.setWordWrap(True)
        self._memory_sub = QLabel(i18n.t("settings.memory_sub"))
        self._memory_sub.setObjectName("sectionSub")
        self._memory_sub.setWordWrap(True)
        self._memory_desc = QLabel(i18n.t("settings.memory_desc"))
        self._memory_desc.setObjectName("sectionDesc")
        self._memory_desc.setWordWrap(True)
        l.addWidget(self._memory_title)
        l.addWidget(self._memory_sub)
        l.addWidget(self._memory_desc)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._memory_refresh_btn = QPushButton(i18n.t("settings.memory_refresh"))
        self._memory_refresh_btn.setObjectName("ghostBtn")
        self._memory_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._memory_refresh_btn.clicked.connect(self._refresh_memory_entries)
        self._memory_clear_btn = QPushButton(i18n.t("settings.memory_clear_all"))
        self._memory_clear_btn.setObjectName("memoryDangerAction")
        self._memory_clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._memory_clear_btn.clicked.connect(self._on_memory_clear_all)
        actions.addWidget(self._memory_refresh_btn)
        actions.addWidget(self._memory_clear_btn)
        actions.addStretch()
        l.addLayout(actions)

        self._memory_status = QLabel("")
        self._memory_status.setObjectName("providerSub")
        self._memory_status.setWordWrap(True)
        l.addWidget(self._memory_status)

        host = QWidget()
        self._memory_entries_host = QVBoxLayout(host)
        self._memory_entries_host.setContentsMargins(0, 0, 0, 0)
        self._memory_entries_host.setSpacing(8)
        l.addWidget(host)
        self._refresh_memory_entries()
        return wrap

    def _clear_memory_rows(self) -> None:
        while self._memory_entries_host.count():
            item = self._memory_entries_host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    @staticmethod
    def _memory_value_preview(entry: dict, limit: int = 160) -> str:
        value = entry.get("value") if isinstance(entry.get("value"), dict) else {}
        data = value.get("data", "")
        if isinstance(data, (dict, list)):
            text = json.dumps(data, ensure_ascii=False, sort_keys=True)
        else:
            text = str(data or "")
        text = " ".join(text.split())
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text

    @staticmethod
    def _memory_sort_key(entry: dict) -> tuple[str, str, str]:
        return (
            str(entry.get("last_used_at") or ""),
            str(entry.get("updated_at") or ""),
            str(entry.get("created_at") or ""),
        )

    @classmethod
    def _memory_entries_for_display(cls, store=None) -> list[dict]:
        entries = (store or MemoryStore()).list_entries()
        return sorted(entries, key=cls._memory_sort_key, reverse=True)

    def _refresh_memory_entries(self) -> None:
        if not hasattr(self, "_memory_entries_host"):
            return
        self._clear_memory_rows()
        try:
            entries = self._memory_entries_for_display()
        except Exception as exc:
            self._memory_status.setText(
                i18n.t("settings.memory_save_failed", error=f": {exc}")
            )
            entries = []
        self._memory_clear_btn.setEnabled(bool(entries))
        if not entries:
            empty = QLabel(i18n.t("settings.memory_empty"))
            empty.setObjectName("providerSub")
            empty.setWordWrap(True)
            self._memory_entries_host.addWidget(empty)
            return
        for entry in entries:
            self._memory_entries_host.addWidget(self._build_memory_row(entry))

    def _build_memory_row(self, entry: dict) -> QFrame:
        row = QFrame()
        row.setObjectName("providerCard")
        outer = QVBoxLayout(row)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        top = QHBoxLayout()
        title = QLabel(str(entry.get("title") or entry.get("key") or "Memory"))
        title.setObjectName("providerName")
        title.setWordWrap(True)
        top.addWidget(title, 1)

        edit_btn = QPushButton(i18n.t("settings.memory_edit"))
        edit_btn.setObjectName("ghostBtn")
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.clicked.connect(
            lambda _=False, e=entry, b=edit_btn: self._on_memory_edit(e, b)
        )
        delete_btn = QPushButton(i18n.t("settings.memory_delete"))
        delete_btn.setObjectName("memoryDangerAction")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.clicked.connect(
            lambda _=False, eid=entry.get("id"), b=delete_btn: self._on_memory_delete(eid, b)
        )
        top.addWidget(edit_btn)
        top.addWidget(delete_btn)
        outer.addLayout(top)

        meta_bits = [
            f"{entry.get('type', 'fact')}/{entry.get('category', 'other')}",
            str(entry.get("source") or "unknown"),
        ]
        stamp = entry.get("last_used_at") or entry.get("updated_at")
        if stamp:
            meta_bits.append(str(stamp))
        meta = QLabel(" · ".join(meta_bits))
        meta.setObjectName("providerSub")
        meta.setWordWrap(True)
        outer.addWidget(meta)

        preview = QLabel(self._memory_value_preview(entry))
        preview.setObjectName("sectionSub")
        preview.setWordWrap(True)
        outer.addWidget(preview)
        return row

    def _on_memory_edit(self, entry: dict, anchor=None) -> None:
        dialog = _MemoryEditDialog(self, entry, anchor=anchor)
        if dialog.exec():
            self._memory_status.setText(i18n.t("settings.memory_saved"))
            self._refresh_memory_entries()

    def _on_memory_delete(self, entry_id, anchor=None) -> None:
        if not entry_id:
            self._memory_status.setText(i18n.t("settings.memory_delete_failed"))
            return
        dialog = _MemoryConfirmDialog(
            self,
            "settings.memory_delete_title",
            "settings.memory_delete_confirm",
            "settings.memory_delete",
            anchor=anchor,
        )
        if not dialog.exec():
            return
        try:
            if not MemoryStore().delete_entry(str(entry_id)):
                raise RuntimeError("memory entry not found")
            self._memory_status.setText(i18n.t("settings.memory_deleted"))
            self._refresh_memory_entries()
        except Exception:
            self._memory_status.setText(i18n.t("settings.memory_delete_failed"))

    def _on_memory_clear_all(self) -> None:
        dialog = _MemoryClearDialog(self, anchor=self._memory_clear_btn)
        if not dialog.exec():
            return
        try:
            MemoryStore().clear()
            self._memory_status.setText(i18n.t("settings.memory_cleared"))
            self._refresh_memory_entries()
        except Exception:
            self._memory_status.setText(
                i18n.t("settings.memory_save_failed", error="")
            )

    # ── MCP / Connectors (WOR-44 Phase 3) ───────────────────────────────────
    # A preconfigured catalog of popular MCP servers (one-tap Connect) plus any
    # user-added custom servers, surfaced through a single connector DROPDOWN
    # (mirrors the AI/voice provider selectors): pick a connector and only THAT
    # one's panel renders below — a live status pill and a Connect/Disconnect
    # toggle. Connecting drives osia.mcp.get_manager() on a BACKGROUND thread
    # (connect() blocks until the handshake completes), so the UI never freezes;
    # a QTimer polls the manager's in-memory status snapshot to flip the visible
    # pill. Non-secret config persists to the ``mcp_servers`` setting; tokens go
    # to the gitignored api_keys.json via set_mcp_token (never to settings.json,
    # never logged). OAuth servers (and transports the manager does not yet
    # support) appear in the dropdown but their panel is disabled until a later
    # phase.

    # Catalog entry: id (mcp__<id>__ namespace — no "__"), display name, short
    # descriptor key, transport, command/args (stdio) or url (http), auth_type,
    # token_env (env var the token is injected into for stdio token servers;
    # remote token servers leave it None and use an in-memory Bearer header),
    # needs_dir (a stdio server needing an allowed-directory arg — the mechanism
    # is retained for custom servers though no catalog entry uses it: Filesystem
    # was removed because it duplicates OSIA's native file_read/bash tools).
    # NOTE: Brave Search and Filesystem are intentionally ABSENT from this
    # catalog — each duplicates a native OSIA capability (Brave Search → native
    # web browsing via Playwright/read_page; Filesystem → native file_read/bash
    # tools), so exposing them as MCP connectors would only add a redundant path.
    # The MCP core stays fully capable; this is purely a curated-catalog choice
    # (a user can still add either as a custom stdio server via "+ Add custom").
    _MCP_CATALOG = [
        {"id": "github", "name": "GitHub", "desc_key": "mcp.desc.github",
         # WOR-99: remote GitHub MCP endpoint documented in Linear WOR-99; PAT
         # rides the existing api_keys.json mcp:github -> Bearer header path.
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://api.githubcopilot.com/mcp/",
         "auth_type": "token", "token_env": None,
         "needs_dir": False},
        {"id": "slack", "name": "Slack", "desc_key": "mcp.desc.slack",
         # WOR-99: official Slack remote MCP endpoint. Slack does not support
         # DCR, so users provide their own Slack app OAuth client id/secret; both
         # are stored in gitignored oauth_tokens.json, never settings.json.
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.slack.com/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False, "oauth_no_dcr": True,
         "oauth_scopes": (
             "search:read.public search:read.private search:read.mpim "
             "search:read.im search:read.files search:read.users chat:write "
             "channels:history groups:history mpim:history im:history "
             "canvases:read canvases:write users:read users:read.email "
             "reactions:write reactions:read emoji:read files:read "
             "channels:write groups:write im:write mpim:write channels:read "
             "groups:read mpim:read"
         ),
         "oauth_token_endpoint_auth_method": "client_secret_post",
         "oauth_issuer": "https://slack.com",
         "oauth_authorization_endpoint": "https://slack.com/oauth/v2_user/authorize",
         "oauth_token_endpoint": "https://slack.com/api/oauth.v2.user.access",
         "oauth_client_id_label_key": "mcp.lbl_client_id_slack",
         "oauth_client_id_ph_key": "mcp.ph_client_id_slack",
         "oauth_client_secret_label_key": "mcp.lbl_client_secret_slack",
         "oauth_client_secret_ph_key": "mcp.ph_client_secret_slack"},
        {"id": "notion", "name": "Notion", "desc_key": "mcp.desc.notion",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.notion.com/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "linear", "name": "Linear", "desc_key": "mcp.desc.linear",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.linear.app/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        # WOR-100: Google Drive, plus the hidden legacy Gmail MCP fallback, use a
        # PRE-REGISTERED OAuth client (model B, per-user) — their MCP servers do
        # NOT support DCR (no registration_endpoint;
        # POST /register → 400). The user pastes a Google "Desktop app" client_id,
        # stored in the gitignored oauth_tokens.json, NEVER mcp_servers; the SDK then
        # skips DCR (see osia.mcp_client.oauth.build_oauth_provider /
        # _seed_static_client_info).
        #
        # ENABLED in WOR-100 Phase 2 (BETA, PC-only loopback; mobile/tunnel later).
        # Phase 2 solved the two end-to-end blockers the Phase-1 probe surfaced by
        # FORCING the OAuth flow at connect (osia.mcp_client.oauth.force_oauth_login,
        # called from manager._serve gated on static_client):
        #   1) Google's servers return HTTP 200 (unauthenticated) on initialize AND
        #      tools/list — they only 401 on a real tool CALL — so the SDK's
        #      401-driven OAuthClientProvider never fired at connect (false CONNECTED,
        #      no token). force_oauth_login drives the authorization-code grant
        #      DIRECTLY on the connect path (OAUTH_CONNECT_TIMEOUT=420 s), so login
        #      completes BEFORE status goes CONNECTED — never via the 25 s tool-call
        #      wrapper that would cancel it.
        #   2) Scope: the SDK overwrites client_metadata.scope with ALL PRM scopes at
        #      its Step 3 — but only INSIDE the 401 branch. Driving the flow directly
        #      bypasses that, so the least-privilege ``oauth_scopes`` below is what
        #      lands in the authorize URL.
        # The well-known Google endpoints are seeded into oauth_metadata at connect
        # (the server 200s, so the SDK never discovers them); they ride the headless
        # reconnect path via mcp_servers (see _mcp_upsert_server / _mcp_build_connect_cfg).
        # ``oauth_token_endpoint_auth_method:"client_secret_post"`` (WOR-100 Phase 2):
        # the real Google login confirmed the "Desktop app" client REQUIRES its
        # client_secret at the token endpoint — a public-PKCE ("none") exchange 400s
        # "client_secret is missing". The user pastes BOTH the client_id AND the
        # client_secret (both stored in the gitignored oauth_tokens.json, NEVER in
        # mcp_servers); the secret rides the token POST via the SDK's
        # prepare_token_auth (client_secret in the request body for client_secret_post).
        {"id": "google-drive", "name": "Google Drive", "desc_key": "mcp.desc.gdrive",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://drivemcp.googleapis.com/mcp/v1", "auth_type": "oauth",
         "token_env": None, "needs_dir": False, "oauth_no_dcr": True,
         # WOR-100: same Google MCP gateway bug (#229) — connectable but badged.
         "temporarily_unavailable": True,
         "oauth_scopes": "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/drive.file",
         "oauth_token_endpoint_auth_method": "client_secret_post",
         "oauth_issuer": "https://accounts.google.com",
         "oauth_authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
         "oauth_token_endpoint": "https://oauth2.googleapis.com/token"},
        # WOR-101 Phase 2: the normal visible Gmail option is native OSIA Gmail
        # REST, not the broken Google Gmail MCP gateway. The legacy MCP code can
        # stay as a future fallback, but this card no longer points users at the
        # legacy Google Gmail MCP endpoint.
        {"id": "gmail", "name": "Gmail", "desc_key": "mcp.desc.gmail",
         "transport": "native", "command": None, "args": [], "url": "",
         "auth_type": "native_oauth", "token_env": None, "needs_dir": False,
         "native_integration": "gmail-rest"},
        {"id": "gmail-mcp-legacy", "name": "Gmail (legacy MCP)",
         "desc_key": "mcp.desc.gmail_legacy",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://gmailmcp.googleapis.com/mcp/v1", "auth_type": "oauth",
         "token_env": None, "needs_dir": False, "oauth_no_dcr": True,
         "temporarily_unavailable": True, "hidden": True,
         "oauth_scopes": "https://mail.google.com/",
         "oauth_token_endpoint_auth_method": "client_secret_post",
         "oauth_issuer": "https://accounts.google.com",
         "oauth_authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
         "oauth_token_endpoint": "https://oauth2.googleapis.com/token"},
        # ── WOR-94: vendor-maintained remote MCP connectors ──────────────────
        # Same OAuth-DCR shape as Notion/Linear above; they inherit the working
        # Dynamic-Client-Registration + mobile-OAuth path with NO new machinery.
        # Transport is streamable_http for all (the manager does not support
        # ``sse`` — see SUPPORTED_TRANSPORTS in osia.mcp_client.manager). URLs
        # verified against official vendor docs + live OAuth metadata (RFC 7591
        # DCR confirmed) as of 2026-06; an entry is connectable iff its ``url``
        # is non-empty (see _mcp_connectable).
        {"id": "sentry", "name": "Sentry", "desc_key": "mcp.desc.sentry",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.sentry.dev/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "stripe", "name": "Stripe", "desc_key": "mcp.desc.stripe",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.stripe.com", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "vercel", "name": "Vercel", "desc_key": "mcp.desc.vercel",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.vercel.com", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "supabase", "name": "Supabase", "desc_key": "mcp.desc.supabase",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.supabase.com/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "neon", "name": "Neon", "desc_key": "mcp.desc.neon",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.neon.tech/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "atlassian", "name": "Atlassian", "desc_key": "mcp.desc.atlassian",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.atlassian.com/v1/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "canva", "name": "Canva", "desc_key": "mcp.desc.canva",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.canva.com/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        {"id": "paypal", "name": "PayPal", "desc_key": "mcp.desc.paypal",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "https://mcp.paypal.com/mcp", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        # ── WOR-94: present but DISABLED (``url`` left empty → _mcp_connectable
        # returns False, exactly like legacy Google Drive above). Each HAS a
        # verified official remote MCP endpoint, but it does NOT support open
        # RFC 7591 Dynamic Client Registration, which OSIA's OAuth substrate
        # REQUIRES (the SDK auto-registers — there is no pre-registered
        # client_id path; see osia.mcp_client.oauth). Fill ``url`` to enable
        # once the connector offers zero-config DCR.
        # TODO(WOR-94): Asana V2 streamable_http endpoint is
        #   https://mcp.asana.com/v2/mcp, but "Dynamic client registration is not
        #   supported with Asana's V2 MCP Server" (clients must pre-register a
        #   client_id). The old /sse beta endpoint was shut down 2026-05-11.
        {"id": "asana", "name": "Asana", "desc_key": "mcp.desc.asana",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        # TODO(WOR-94): HubSpot endpoint is https://mcp.hubspot.com (bare host,
        #   streamable_http) but exposes NO registration_endpoint — the user must
        #   manually create an "MCP Auth App" for a client_id/secret (no DCR).
        {"id": "hubspot", "name": "HubSpot", "desc_key": "mcp.desc.hubspot",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
        # TODO(WOR-94): Figma endpoint is https://mcp.figma.com/mcp (beta,
        #   streamable_http) but its DCR registration endpoint returns 403 for
        #   non-allowlisted clients (the mcp:connect scope is gated to Figma's
        #   pre-approved MCP catalog). Enable once OSIA is allowlisted by Figma.
        {"id": "figma", "name": "Figma", "desc_key": "mcp.desc.figma",
         "transport": "streamable_http", "command": None, "args": [],
         "url": "", "auth_type": "oauth",
         "token_env": None, "needs_dir": False},
    ]

    # WOR-92: connector IDENTITY (id/name/desc_key) has ONE source of truth in
    # the Qt-free osia.mcp_client.catalog — the module the agent's connector-
    # awareness prompt reads SERVER-SIDE, so PC and mobile agree. _MCP_CATALOG
    # above mirrors those values and adds the GUI-only transport/url/auth/oauth_*
    # connection fields. Assert at import that the two cannot DRIFT: a mismatch
    # fails fast in development (the GUI won't start) so it can never ship.
    # Import-guarded so a missing module degrades gracefully; a real drift raises
    # loudly. Plain loops (NOT comprehensions) on purpose — a class-body
    # comprehension cannot see class-body locals (Python class-scope rule).
    try:
        from osia.mcp_client.catalog import catalog_by_id as _wor92_by_id
        _wor92_meta = _wor92_by_id()
        _wor92_gui_ids = set()
        _wor92_drift = []
        for _wor92_e in _MCP_CATALOG:
            _wor92_gui_ids.add(_wor92_e["id"])
            _wor92_m = _wor92_meta.get(_wor92_e["id"])
            if (_wor92_m is None
                    or _wor92_m.get("name") != _wor92_e["name"]
                    or _wor92_m.get("desc_key") != _wor92_e["desc_key"]):
                _wor92_drift.append(_wor92_e["id"])
        _wor92_missing = []
        for _wor92_mid in _wor92_meta:
            if _wor92_mid not in _wor92_gui_ids:
                _wor92_missing.append(_wor92_mid)
        if _wor92_drift or _wor92_missing:
            raise RuntimeError(
                "WOR-92: _MCP_CATALOG drifted from osia.mcp_client.catalog "
                f"(identity mismatch={_wor92_drift}, missing_in_gui="
                f"{_wor92_missing}); keep id/name/desc_key in sync — catalog.py "
                "is the single source."
            )
        # Success path: drop the scratch names so they aren't left as class
        # attributes (a drift instead propagates the RuntimeError, by design).
        del (_wor92_by_id, _wor92_meta, _wor92_gui_ids, _wor92_drift,
             _wor92_e, _wor92_m, _wor92_missing, _wor92_mid)
    except ImportError:
        pass

    # (background, foreground) per status — inline-styled so no global QSS edit.
    def _build_mcp_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._mcp_title = QLabel(i18n.t("settings.mcp"))
        self._mcp_title.setObjectName("sectionTitle")
        self._mcp_title.setWordWrap(True)
        self._mcp_sub = QLabel(i18n.t("settings.mcp_sub"))
        self._mcp_sub.setObjectName("sectionSub")
        self._mcp_sub.setWordWrap(True)
        self._mcp_desc = QLabel(i18n.t("settings.mcp_desc"))
        self._mcp_desc.setObjectName("sectionDesc")
        self._mcp_desc.setWordWrap(True)
        l.addWidget(self._mcp_title)
        l.addWidget(self._mcp_sub)
        l.addWidget(self._mcp_desc)

        # WOR-44 P3 (refine) — connector DROPDOWN instead of a long stacked card
        # list (mirrors the AI/voice provider selectors): a label + combo row,
        # then ONE panel below for the selected connector only. Keeping a single
        # panel live at a time keeps the page compact (no width/height blowout
        # past POPUP_W / the 640 max) and means the status poll only ever
        # repaints the one visible pill.
        sel_row = QHBoxLayout()
        self._mcp_select_label = QLabel(i18n.t("mcp.select_label"))
        self._mcp_select_label.setObjectName("fieldLabel")
        self._mcp_select_label.setMinimumWidth(96)
        self._mcp_select_label.setWordWrap(True)
        self._mcp_combo = QComboBox()
        self._mcp_combo.setObjectName("voiceCombo")
        _cap_combo_width(self._mcp_combo)   # never let a long name inflate width
        self._mcp_combo.currentIndexChanged.connect(self._on_mcp_select)
        sel_row.addWidget(self._mcp_select_label)
        sel_row.addWidget(self._mcp_combo, 1)
        l.addLayout(sel_row)

        # Single selected-connector panel host (rebuilt in place on selection
        # change / add / remove / language switch — never a stacked list).
        panel_wrap = QWidget()
        self._mcp_panel_host = QVBoxLayout(panel_wrap)
        self._mcp_panel_host.setContentsMargins(0, 0, 0, 0)
        self._mcp_panel_host.setSpacing(8)
        l.addWidget(panel_wrap)

        # Per-id widget refs — only the SELECTED connector's widgets are live at
        # a time (the panel holds one connector); handlers index by id.
        self._mcp_pills: dict = {}
        self._mcp_buttons: dict = {}
        self._mcp_token_inputs: dict = {}
        self._mcp_client_id_inputs: dict = {}   # WOR-100: non-DCR OAuth client_id
        self._mcp_client_secret_inputs: dict = {}   # WOR-100 Ph2: non-DCR client_secret
        self._mcp_dir_inputs: dict = {}
        self._mcp_extra_inputs: dict = {}   # {id: {env_var: QLineEdit}}
        self._gmail_rest_account_labels: dict = {}
        self._gmail_rest_capability_badges: dict = {}
        self._gmail_rest_capability_labels: dict = {}
        self._gmail_rest_upgrade_buttons: dict = {}
        self._gmail_rest_upgrade_info_badges: dict = {}
        # Optimistic transient state per id: {id: (visual_state, resolve_set, ticks)}
        self._mcp_pending: dict = {}
        # Catalog + custom entries by id (rebuilt by _render_mcp) and the
        # currently-selected connector id (UI-only state; survives rebuilds).
        self._mcp_entries: dict = {}
        self._mcp_selected_id = None

        self._mcp_add_btn = QPushButton("＋ " + i18n.t("mcp.add_custom"))
        self._mcp_add_btn.setObjectName("primaryBtn")
        self._mcp_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mcp_add_btn.clicked.connect(self._on_mcp_add_custom)
        l.addWidget(self._mcp_add_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return wrap

    def _mcp_connectable(self, entry: dict) -> bool:
        """A server is connectable now iff the manager supports its transport
        AND, for OAuth servers (WOR-44 Phase 5, 5-oauth-a), it has a non-empty
        URL (the OAuth flow needs an endpoint). Kept in lockstep with the
        server's _mcp_connectable (osia/server/server.py) so PC and mobile agree
        on what the manager can connect. (Any URL-less OAuth catalog entry —
        e.g. the WOR-94 vendors shipped DISABLED with url="" — therefore stays
        non-connectable, showing the disabled OAuth label, until its URL is
        wired. Google Drive was enabled in WOR-100 Phase 2; visible Gmail is now
        native REST and handled by its own branch.)"""
        try:
            from osia.mcp_client import SUPPORTED_TRANSPORTS
        except Exception:
            SUPPORTED_TRANSPORTS = ("stdio",)
        if self._is_gmail_rest_entry(entry):
            return True
        return (
            entry.get("transport") in SUPPORTED_TRANSPORTS
            and (
                entry.get("auth_type") != "oauth"
                or bool(str(entry.get("url") or "").strip())
            )
        )

    @staticmethod
    def _is_gmail_rest_entry(entry: dict | None) -> bool:
        return bool(
            isinstance(entry, dict)
            and entry.get("id") == "gmail"
            and entry.get("native_integration") == "gmail-rest"
        )

    def _make_google_oauth_guide_button(
        self, entry: dict, *, mode: str | None = None
    ) -> QPushButton:
        guide_btn = QPushButton(i18n.t("mcp.setup_guide"))
        guide_btn.setObjectName("ghostBtn")
        guide_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        def _open_oauth_wizard(_=False, e=entry, wizard_mode=mode):
            try:
                from osia.gui.mcp_oauth_wizard import _MCPOAuthWizardDialog
                _MCPOAuthWizardDialog(self, e, mode=wizard_mode).exec()
            except Exception:
                pass

        guide_btn.clicked.connect(_open_oauth_wizard)
        return guide_btn

    def _gmail_rest_client(self) -> dict:
        """Return stored native Gmail client fields, falling back to legacy setup."""
        for key in ("gmail-rest", "gmail"):
            try:
                client = get_oauth_client(key)
            except Exception:
                client = {}
            if isinstance(client, dict) and client.get("client_id"):
                return dict(client)
        return {}

    def _save_gmail_rest_client_from_fields(self) -> None:
        cidin = self._mcp_client_id_inputs.get("gmail")
        csecin = self._mcp_client_secret_inputs.get("gmail")
        client_id = cidin.text().strip() if cidin else ""
        client_secret = csecin.text().strip() if csecin else ""
        if not client_id and not client_secret:
            return
        if not client_id:
            self._mcp_flag_required(cidin)
            raise ValueError("missing Gmail OAuth client id")
        existing = self._gmail_rest_client()
        if (
            not client_secret
            and isinstance(existing, dict)
            and str(existing.get("client_id") or "").strip() == client_id
            and str(existing.get("client_secret") or "").strip()
        ):
            client_secret = str(existing.get("client_secret") or "").strip()
        redirect_uris = ["http://127.0.0.1:8975/callback"]
        try:
            from osia.integrations.google_oauth import mobile_redirect_uri

            mobile_callback = mobile_redirect_uri()
            if mobile_callback:
                redirect_uris.append(mobile_callback)
        except Exception:
            pass
        client = {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": (
                "client_secret_post" if client_secret else "none"
            ),
        }
        if client_secret:
            client["client_secret"] = client_secret
        set_oauth_client("gmail-rest", client)

    def _mcp_saved_servers(self) -> list:
        out = []
        for s in (get_setting("mcp_servers", []) or []):
            if isinstance(s, dict) and s.get("id"):
                out.append(s)
        return out

    def _mcp_upsert_server(self, cfg: dict) -> None:
        """Persist a NON-secret server config into the mcp_servers setting
        (replace by id, else append). Never stores tokens (env is {})."""
        clean = {
            "id": cfg["id"], "name": cfg.get("name", cfg["id"]),
            "transport": cfg.get("transport", "stdio"),
            "command": cfg.get("command"), "args": list(cfg.get("args") or []),
            "url": cfg.get("url"), "env": {},
            "auth_type": cfg.get("auth_type", "none"),
            "enabled": bool(cfg.get("enabled", False)),
            "token_env": cfg.get("token_env"),
            # Non-secret extra env values for custom stdio servers round-trip here so
            # the field re-prefills on reopen; tokens NEVER live here.
            "extra_env": dict(cfg.get("extra_env") or {}),
            "custom": bool(cfg.get("custom", False)),
        }
        # WOR-100: carry the NON-secret non-DCR markers so the headless reconnect
        # path (reconcile/boot → build_headless_connect_cfg → dict(saved)) takes the
        # pre-registered-client path after a restart. Added ONLY for non-DCR
        # connectors, so ordinary DCR entries keep their exact persisted shape. The
        # client_id/secret are NOT here — they live in the gitignored oauth_tokens.json.
        if cfg.get("oauth_no_dcr"):
            clean["oauth_no_dcr"] = True
            clean["oauth_scopes"] = cfg.get("oauth_scopes") or None
            clean["oauth_token_endpoint_auth_method"] = (
                cfg.get("oauth_token_endpoint_auth_method") or None
            )
            # WOR-100 Phase 2: the well-known AS endpoints (non-secret) so the
            # headless reconnect path (build_headless_connect_cfg = dict(saved))
            # can seed oauth_metadata and force the flow after a restart.
            clean["oauth_issuer"] = cfg.get("oauth_issuer") or None
            clean["oauth_authorization_endpoint"] = (
                cfg.get("oauth_authorization_endpoint") or None
            )
            clean["oauth_token_endpoint"] = cfg.get("oauth_token_endpoint") or None
        servers = [s for s in self._mcp_saved_servers() if s.get("id") != cfg["id"]]
        servers.append(clean)
        set_setting("mcp_servers", servers)

    def _mcp_set_enabled(self, server_id: str, enabled: bool) -> None:
        servers = self._mcp_saved_servers()
        changed = False
        for s in servers:
            if s.get("id") == server_id:
                s["enabled"] = enabled
                changed = True
        if changed:
            set_setting("mcp_servers", servers)

    def _mcp_remove_saved(self, server_id: str) -> None:
        servers = [s for s in self._mcp_saved_servers() if s.get("id") != server_id]
        set_setting("mcp_servers", servers)

    def _mcp_all_entries(self) -> list:
        """Ordered connector entries for the dropdown: every catalog server,
        then any user-added custom (non-catalog) saved servers. (Filesystem and
        Brave Search are intentionally absent from the catalog — each duplicates
        a native OSIA capability; see _MCP_CATALOG.)"""
        catalog_ids = {e["id"] for e in self._MCP_CATALOG}
        out = [e for e in self._MCP_CATALOG if not e.get("hidden")]
        for s in self._mcp_saved_servers():
            sid = s["id"]
            if sid in catalog_ids:
                continue
            out.append({
                "id": sid, "name": s.get("name", sid),
                "transport": s.get("transport", "stdio"),
                "command": s.get("command"), "args": list(s.get("args") or []),
                "url": s.get("url"), "auth_type": s.get("auth_type", "none"),
                "token_env": s.get("token_env"), "needs_dir": False,
                "custom": True, "desc_key": None,
            })
        return out

    def _render_mcp(self) -> None:
        """Repopulate the connector dropdown (catalog + custom) and render the
        selected connector's panel. Safe to call repeatedly (load, add, remove,
        language change). The poll then tracks every connector's status via the
        manager snapshot, but only the visible pill is painted."""
        entries = self._mcp_all_entries()
        self._mcp_entries = {e["id"]: e for e in entries}
        combo = self._mcp_combo
        combo.blockSignals(True)
        combo.clear()
        for e in entries:
            combo.addItem(e.get("name", e["id"]), e["id"])
        # Keep the prior selection if it still exists, else default to the first.
        sel = self._mcp_selected_id
        if sel not in self._mcp_entries:
            sel = entries[0]["id"] if entries else None
        self._mcp_selected_id = sel
        if sel is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == sel:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        self._render_mcp_panel(sel)

    def _render_mcp_panel(self, sid) -> None:
        """Rebuild the single selected-connector panel in place, resetting the
        per-id widget refs to just this connector (only one panel is ever live).
        Reuses _build_mcp_card so every field / token / extra-env / Connect path
        is byte-identical to before — only how many cards exist changed."""
        host = self._mcp_panel_host
        while host.count():
            item = host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._mcp_pills = {}
        self._mcp_buttons = {}
        self._mcp_cancel_buttons = {}   # WOR-97: OAuth "Cancel" btns (per id)
        self._mcp_switch_buttons = {}   # WOR-98: OAuth "Switch account" btns (per id)
        self._mcp_token_inputs = {}
        self._mcp_client_id_inputs = {}   # WOR-100: non-DCR OAuth client_id field
        self._mcp_client_secret_inputs = {}   # WOR-100 Ph2: non-DCR client_secret field
        self._mcp_dir_inputs = {}
        self._mcp_extra_inputs = {}
        self._gmail_rest_account_labels = {}
        self._gmail_rest_capability_badges = {}
        self._gmail_rest_capability_labels = {}
        self._gmail_rest_upgrade_buttons = {}
        self._gmail_rest_upgrade_info_badges = {}
        entry = self._mcp_entries.get(sid)
        if entry is None:
            return
        saved = next(
            (s for s in self._mcp_saved_servers() if s.get("id") == sid), None
        )
        host.addWidget(
            self._build_mcp_card(entry, saved, bool(entry.get("custom")))
        )
        self._refresh_mcp_status()

    def _on_mcp_select(self, _idx: int) -> None:
        sid = self._mcp_combo.currentData()
        self._mcp_selected_id = sid
        self._render_mcp_panel(sid)

    def _build_mcp_card(self, entry: dict, saved: dict, is_custom: bool) -> QWidget:
        sid = entry["id"]
        row = QFrame()
        row.setObjectName("providerCard")
        v = QVBoxLayout(row)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(6)

        # Header — name + status pill.
        head = QHBoxLayout()
        head.setSpacing(6)
        name = QLabel(entry.get("name", sid))
        name.setObjectName("providerName")
        head.addWidget(name)
        # WOR-100: a connector flagged ``temporarily_unavailable`` (Gmail/Drive —
        # the Google MCP gateway has a server-side scope bug, #229) shows a subtle
        # badge + a hover tooltip making clear it's Google's issue, not OSIA's.
        # Added INLINE in the existing header (before the stretch) — no new row,
        # so card layout/sizing is unchanged; gated, so other connectors are
        # byte-for-byte identical.
        if entry.get("temporarily_unavailable"):
            badge = QLabel(i18n.t("mcp.badge_unavailable"))
            badge.setObjectName("providerSub")
            badge.setStyleSheet(
                f"color: {self._theme.palette.warning}; font-size: 11px;"
            )
            badge.setToolTip(i18n.t("mcp.tooltip_google_bug"))
            head.addWidget(badge)
        if self._is_gmail_rest_entry(entry):
            badge = QLabel(i18n.t("mcp.badge_readonly"))
            badge.setObjectName("providerSub")
            badge.setStyleSheet("font-size: 11px;")
            self._gmail_rest_capability_badges[sid] = badge
            head.addWidget(badge)
        head.addStretch(1)
        pill = QLabel("")
        pill.setObjectName("mcpPill")
        self._set_mcp_pill(pill, "disconnected")
        self._mcp_pills[sid] = pill
        head.addWidget(pill)
        v.addLayout(head)

        # Descriptor.
        if is_custom:
            target = entry.get("command") or entry.get("url") or ""
            desc_txt = f"{entry.get('transport', 'stdio')} · {target}"
        else:
            desc_txt = i18n.t(entry["desc_key"]) if entry.get("desc_key") else ""
        if desc_txt:
            desc = QLabel(desc_txt)
            desc.setObjectName("providerSub")
            desc.setWordWrap(True)
            v.addWidget(desc)

        if self._is_gmail_rest_entry(entry):
            account = QLabel("")
            account.setObjectName("providerSub")
            account.setWordWrap(True)
            self._gmail_rest_account_labels[sid] = account
            v.addWidget(account)
            self._update_gmail_rest_account_label()

            capability = QLabel("")
            capability.setObjectName("providerSub")
            capability.setWordWrap(True)
            self._gmail_rest_capability_labels[sid] = capability
            v.addWidget(capability)
            self._update_gmail_rest_capability_label()

            client = self._gmail_rest_client()
            client_configured = bool(client.get("client_id"))
            show_credential_fields = (
                not client_configured or self._gmail_rest_credentials_editing
            )
            if show_credential_fields:
                cidlab = QLabel(i18n.t("mcp.lbl_client_id"))
                cidlab.setObjectName("providerSub")
                cidlab.setWordWrap(True)
                v.addWidget(cidlab)
                cidin = QLineEdit(str(client.get("client_id") or ""))
                cidin.setObjectName("keyInput")
                cidin.setPlaceholderText(i18n.t("mcp.gmail_ph_client_id"))
                self._mcp_client_id_inputs[sid] = cidin
                v.addWidget(cidin)

                cseclab = QLabel(i18n.t("mcp.lbl_client_secret"))
                cseclab.setObjectName("providerSub")
                cseclab.setWordWrap(True)
                v.addWidget(cseclab)
                csecin = QLineEdit("")
                csecin.setObjectName("keyInput")
                csecin.setEchoMode(QLineEdit.EchoMode.Password)
                csecin.setPlaceholderText(
                    i18n.t("mcp.ph_client_secret_replace")
                    if client_configured
                    else i18n.t("mcp.gmail_ph_client_secret")
                )
                self._mcp_client_secret_inputs[sid] = csecin
                v.addWidget(csecin)
            else:
                configured = QLabel(i18n.t("mcp.gmail_credentials_configured"))
                configured.setObjectName("providerSub")
                configured.setWordWrap(True)
                v.addWidget(configured)

                cred_actions = QHBoxLayout()
                cred_actions.setSpacing(6)
                edit_btn = QPushButton(i18n.t("mcp.gmail_edit_credentials"))
                edit_btn.setObjectName("ghostBtn")
                edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)

                def _edit_gmail_credentials(_=False):
                    self._gmail_rest_credentials_editing = True
                    self._render_mcp_panel("gmail")

                edit_btn.clicked.connect(_edit_gmail_credentials)
                cred_actions.addWidget(edit_btn)
                cred_actions.addStretch(1)
                v.addLayout(cred_actions)

            v.addWidget(self._make_google_oauth_guide_button(entry, mode="gmail-rest"))

        connectable = self._mcp_connectable(entry)

        # Filesystem needs an allowed directory. Default to ~/Documents when it
        # exists (NEVER the whole home dir — that would expose ~/.osia, which
        # holds api_keys.json); otherwise leave empty to force a deliberate
        # choice. The model can read/write everything under the chosen folder.
        if entry.get("needs_dir"):
            saved_args = (saved or {}).get("args") or []
            base = len(entry.get("args") or [])
            if len(saved_args) > base:
                cur_dir = saved_args[base]
            else:
                docs = Path.home() / "Documents"
                cur_dir = str(docs) if docs.is_dir() else ""
            dlab = QLabel(i18n.t("mcp.lbl_dir"))
            dlab.setObjectName("providerSub")
            v.addWidget(dlab)
            din = QLineEdit(cur_dir)
            din.setObjectName("keyInput")
            din.setPlaceholderText(i18n.t("mcp.ph_dir"))
            self._mcp_dir_inputs[sid] = din
            v.addWidget(din)

        # Token + extra-env fields only for CONNECTABLE token servers — a field
        # the user can't act on (a disabled OAuth/HTTP card) would be misleading
        # and its value would never be saved.
        if connectable and entry.get("auth_type") == "token":
            tlab = QLabel(i18n.t("mcp.lbl_token"))
            tlab.setObjectName("providerSub")
            v.addWidget(tlab)
            tin = QLineEdit(get_mcp_token(sid))
            tin.setObjectName("keyInput")
            tin.setEchoMode(QLineEdit.EchoMode.Password)
            tin.setPlaceholderText(i18n.t("mcp.ph_token"))
            self._mcp_token_inputs[sid] = tin
            v.addWidget(tin)

        # A PRE-REGISTERED (non-DCR) OAuth connector needs the user's own OAuth
        # client_id. Rendered like the token field, but the value is stored in the
        # gitignored oauth_tokens.json (under the per-server ``static_client``
        # blob) — NEVER in mcp_servers. Only for connectable oauth_no_dcr cards
        # (a field on a disabled card would be misleading and never saved).
        if connectable and entry.get("auth_type") == "oauth" and entry.get("oauth_no_dcr"):
            cidlab = QLabel(i18n.t(
                entry.get("oauth_client_id_label_key") or "mcp.lbl_client_id"
            ))
            cidlab.setObjectName("providerSub")
            cidlab.setWordWrap(True)
            v.addWidget(cidlab)
            try:
                cur_cid = str(
                    ((get_mcp_oauth(sid) or {}).get("static_client") or {}).get("client_id") or ""
                )
            except Exception:
                cur_cid = ""
            cidin = QLineEdit(cur_cid)
            cidin.setObjectName("keyInput")
            cidin.setPlaceholderText(i18n.t(
                entry.get("oauth_client_id_ph_key") or "mcp.ph_client_id"
            ))
            self._mcp_client_id_inputs[sid] = cidin
            v.addWidget(cidin)
            # Some pre-registered clients require a client_secret at the token
            # endpoint (oauth_token_endpoint_auth_method=client_secret_post), so
            # a MASKED secret field sits next to the client_id. Like the
            # client_id, the value lives in the gitignored oauth_tokens.json (the
            # per-server ``static_client`` blob) — NEVER in mcp_servers.
            cseclab = QLabel(i18n.t(
                entry.get("oauth_client_secret_label_key")
                or "mcp.lbl_client_secret"
            ))
            cseclab.setObjectName("providerSub")
            cseclab.setWordWrap(True)
            v.addWidget(cseclab)
            try:
                cur_csec = str(
                    ((get_mcp_oauth(sid) or {}).get("static_client") or {}).get("client_secret") or ""
                )
            except Exception:
                cur_csec = ""
            csecin = QLineEdit(cur_csec)
            csecin.setObjectName("keyInput")
            csecin.setEchoMode(QLineEdit.EchoMode.Password)
            csecin.setPlaceholderText(i18n.t(
                entry.get("oauth_client_secret_ph_key")
                or "mcp.ph_client_secret"
            ))
            self._mcp_client_secret_inputs[sid] = csecin
            v.addWidget(csecin)
            # WOR-100 Phase 2: the Google setup guide is provider-specific, so it
            # stays only on Gmail/Drive. Slack has its own app setup flow.
            if sid in ("google-drive", "gmail"):
                v.addWidget(self._make_google_oauth_guide_button(entry))

        if connectable:
            saved_extra = (saved or {}).get("extra_env") or {}
            for spec in entry.get("extra_env") or []:
                var = spec.get("var")
                if not var:
                    continue
                elab = QLabel(i18n.t(spec.get("label_key", "")))
                elab.setObjectName("providerSub")
                v.addWidget(elab)
                ein = QLineEdit(str(saved_extra.get(var, "")))
                ein.setObjectName("keyInput")
                if spec.get("ph_key"):
                    ein.setPlaceholderText(i18n.t(spec["ph_key"]))
                self._mcp_extra_inputs.setdefault(sid, {})[var] = ein
                v.addWidget(ein)

        # Action row.
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addStretch(1)
        if connectable:
            if self._is_gmail_rest_entry(entry):
                upgrade_btn = QPushButton(i18n.t("mcp.gmail_upgrade"))
                upgrade_btn.setObjectName("ghostBtn")
                upgrade_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                upgrade_btn.clicked.connect(self._on_gmail_rest_upgrade_permissions)
                upgrade_btn.setVisible(False)
                self._gmail_rest_upgrade_buttons[sid] = upgrade_btn
                actions.addWidget(upgrade_btn)
                upgrade_info = _InfoBadge("mcp.gmail_upgrade_hint")
                upgrade_info.setVisible(False)
                self._gmail_rest_upgrade_info_badges[sid] = upgrade_info
                actions.addWidget(upgrade_info)
                btn = QPushButton(i18n.t("mcp.gmail_connect"))
                btn.setObjectName("primaryBtn")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(
                    lambda _=False, e=entry: self._on_gmail_rest_toggle(e)
                )
                self._mcp_buttons[sid] = btn
                actions.addWidget(btn)
                v.addLayout(actions)
                self._refresh_gmail_rest_status()
                return row
            # WOR-44 Phase 5 (5-oauth-a): OAuth connectors get a distinct
            # "Connect with OAuth" label (the click still routes through the
            # same _on_mcp_toggle → _on_mcp_connect path; the manager opens the
            # browser and the loopback callback completes the login).
            connect_label = (
                i18n.t("mcp.connect_oauth")
                if entry.get("auth_type") == "oauth"
                else i18n.t("mcp.connect")
            )
            btn = QPushButton(connect_label)
            btn.setObjectName("primaryBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, e=entry: self._on_mcp_toggle(e))
            self._mcp_buttons[sid] = btn
            actions.addWidget(btn)
            # WOR-97: a "Cancel" control for OAuth connectors only, shown ONLY
            # while the pill is "connecting" (an interactive login can be
            # abandoned — create-account/closed-browser/Error 1033). Built hidden;
            # _refresh_mcp_status toggles its visibility per tick on the pill state.
            if entry.get("auth_type") == "oauth":
                cancel_btn = QPushButton(i18n.t("mcp.cancel"))
                cancel_btn.setObjectName("ghostBtn")
                cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                cancel_btn.clicked.connect(
                    lambda _=False, e=entry: self._on_mcp_cancel(e)
                )
                cancel_btn.setVisible(False)
                self._mcp_cancel_buttons[sid] = cancel_btn
                actions.addWidget(cancel_btn)
                # WOR-98: a "Switch account" control for OAuth connectors only,
                # shown ONLY while the pill is "connected" (revoke the grant +
                # forget the DCR client → next Connect re-runs DCR + a fresh
                # login / different account). Built hidden; _refresh_mcp_status
                # toggles its visibility per tick on the pill state (never shown
                # at the same time as Cancel, which is "connecting"-only).
                switch_btn = QPushButton(i18n.t("mcp.switch_account"))
                switch_btn.setObjectName("ghostBtn")
                switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                switch_btn.clicked.connect(
                    lambda _=False, e=entry: self._on_mcp_switch_account(e)
                )
                switch_btn.setVisible(False)
                self._mcp_switch_buttons[sid] = switch_btn
                actions.addWidget(switch_btn)
        else:
            reason = "mcp.oauth_soon" if entry.get("auth_type") == "oauth" else "mcp.transport_soon"
            soon = QPushButton(i18n.t(reason))
            soon.setObjectName("ghostBtn")
            soon.setEnabled(False)
            actions.addWidget(soon)
        if is_custom:
            rm = QPushButton("✕")
            rm.setObjectName("convDel")
            rm.setFixedSize(28, 28)
            rm.setCursor(Qt.CursorShape.PointingHandCursor)
            rm.setToolTip(i18n.t("mcp.remove"))
            rm.clicked.connect(lambda _=False, i=sid: self._on_mcp_remove_custom(i))
            actions.addWidget(rm)
        v.addLayout(actions)
        return row

    def _set_mcp_pill(self, pill: QLabel, state: str) -> None:
        if state not in {"connected", "connecting", "failed", "disconnected"}:
            state = "disconnected"
        # Re-apply ONLY when the state actually changed. The status poll calls
        # this every ~1.2s; re-setting an identical stylesheet forces Qt to
        # re-parse it each tick — the source of the "Could not parse stylesheet"
        # console spam. The last-applied state is cached on the pill itself; a
        # freshly rebuilt pill has no cache, so a new panel always paints once,
        # and a language switch rebuilds the pill so its text re-translates.
        if getattr(pill, "_mcp_state", None) == state:
            return
        pill._mcp_state = state
        palette = self._theme.palette
        colors = {
            "connected": (palette.success, palette.background),
            "connecting": (palette.warning, palette.background),
            "failed": (palette.error, palette.background),
            "disconnected": (palette.border, palette.muted_text),
        }
        bg, fg = colors[state]
        pill.setText(i18n.t("mcp.status_" + state))
        # Single closing brace: the trailing fragment was previously a NON-f
        # literal ending in "}}", so Qt saw a stray extra "}" and rejected the
        # whole rule as invalid QSS. One "}" closes the f-string's "{{" block.
        pill.setStyleSheet(
            f"QLabel{{background:{bg};color:{fg};border-radius:4px;"
            "padding:2px 9px;font-size:11px;font-weight:600;}"
        )

    @staticmethod
    def _mcp_flag_required(widget) -> None:
        """Outline a required-but-empty input in red so a no-op Connect is not
        silent. Cleared on the next render."""
        if widget is not None:
            error = get_theme_manager().palette.error
            widget.setStyleSheet(f"QLineEdit{{border:1px solid {error};}}")

    def _mcp_build_connect_cfg(self, entry: dict):
        """Return ``(connect_cfg, persist_cfg, token, client_id, client_secret)``.
        ``connect_cfg`` carries the token + any extra env in ``env`` (in-memory only,
        passed to the manager); ``persist_cfg`` never carries secrets (``env`` is
        always {} — non-secret extra-env values round-trip under ``extra_env``).
        ``client_id`` / ``client_secret`` (WOR-100) are the user's pre-registered
        OAuth credentials for a non-DCR connector (Google) — stored by the caller in
        the gitignored oauth_tokens.json, NEVER in persist_cfg/mcp_servers; "" for
        every other connector (and ``client_secret`` stays "" for a public-PKCE
        ``none`` client). Returns ``(None, None, "", "", "")`` on a validation
        failure (an empty required field is flagged red)."""
        sid = entry["id"]
        args = list(entry.get("args") or [])
        if entry.get("needs_dir"):
            din = self._mcp_dir_inputs.get(sid)
            d = din.text().strip() if din else ""
            if not d:
                # Never silently fall back to the whole home dir — require a
                # deliberate choice.
                self._mcp_flag_required(din)
                return None, None, "", "", ""
            args = args + [d]

        # Extra (non-secret) env fields for custom stdio servers.
        extra_env: dict = {}
        for spec in entry.get("extra_env") or []:
            var = spec.get("var")
            ein = self._mcp_extra_inputs.get(sid, {}).get(var)
            val = ein.text().strip() if ein else ""
            if not val and spec.get("required"):
                self._mcp_flag_required(ein)
                return None, None, "", "", ""
            if val:
                extra_env[var] = val

        persist = {
            "id": sid, "name": entry.get("name", sid),
            "transport": entry.get("transport", "stdio"),
            "command": entry.get("command"), "args": args,
            "url": entry.get("url"), "env": {},
            "auth_type": entry.get("auth_type", "none"), "enabled": True,
            "token_env": entry.get("token_env"),
            "extra_env": extra_env,
            "custom": bool(entry.get("custom", False)),
        }

        # WOR-100: carry the NON-secret non-DCR markers into persist_cfg so the
        # headless reconnect path (reconcile/boot, which reads mcp_servers via
        # build_headless_connect_cfg → dict(saved)) also takes the pre-registered
        # client path after a restart. These are non-secret (a bool, public scope
        # strings, an auth-method literal); the client_id/secret NEVER go here.
        if entry.get("oauth_no_dcr"):
            persist["oauth_no_dcr"] = True
            persist["oauth_scopes"] = entry.get("oauth_scopes") or None
            persist["oauth_token_endpoint_auth_method"] = (
                entry.get("oauth_token_endpoint_auth_method") or None
            )
            # WOR-100 Phase 2: the well-known AS endpoints (non-secret) used by
            # force_oauth_login to seed oauth_metadata at connect. In persist so
            # both connect_cfg (= dict(persist)) and the saved settings carry them.
            persist["oauth_issuer"] = entry.get("oauth_issuer") or None
            persist["oauth_authorization_endpoint"] = (
                entry.get("oauth_authorization_endpoint") or None
            )
            persist["oauth_token_endpoint"] = entry.get("oauth_token_endpoint") or None

        # WOR-100: a non-DCR OAuth connector requires the user's client_id. It is
        # AUTHORITATIVE from the field (empty → flag red + abort, like the dir /
        # extra-env required gates); the caller stores it in the gitignored
        # oauth_tokens.json, never in persist_cfg. WOR-100 Phase 2: a
        # ``client_secret_post`` connector (Google "Desktop app") ALSO needs its
        # client_secret at the token endpoint, so it is gated the SAME way (empty →
        # flag red + abort); a public-PKCE ``none`` client leaves it blank. The
        # secret comes from the masked field and is stored by the caller in the SAME
        # gitignored oauth_tokens.json — never in persist_cfg.
        client_id = ""
        client_secret = ""
        if entry.get("oauth_no_dcr"):
            cidin = self._mcp_client_id_inputs.get(sid)
            client_id = cidin.text().strip() if cidin else ""
            if not client_id:
                self._mcp_flag_required(cidin)
                return None, None, "", "", ""
            csecin = self._mcp_client_secret_inputs.get(sid)
            client_secret = csecin.text().strip() if csecin else ""
            if (
                str(entry.get("oauth_token_endpoint_auth_method") or "none")
                in ("client_secret_post", "client_secret_basic")
                and not client_secret
            ):
                self._mcp_flag_required(csecin)
                return None, None, "", "", ""

        # Build the in-memory connect env: the token (authoritative from the
        # field — empty means none) plus any extra-env values.
        env: dict = {}
        headers: dict = {}
        token = ""
        if entry.get("auth_type") == "token":
            tin = self._mcp_token_inputs.get(sid)
            token = tin.text().strip() if tin else ""
            if token and entry.get("token_env"):
                env[entry["token_env"]] = token
            # WOR-44 Phase 5: a streamable-HTTP server carries its token in an
            # in-memory Authorization header (no subprocess env). Built only for
            # the manager handshake — persist_cfg never gets a 'headers' key, so
            # the secret never reaches the git-tracked settings.json.
            if token and entry.get("transport") == "streamable_http":
                headers["Authorization"] = f"Bearer {token}"
        env.update(extra_env)
        connect = dict(persist)
        if env:
            connect["env"] = env
        if headers:
            connect["headers"] = headers
        return connect, persist, token, client_id, client_secret

    def _refresh_gmail_rest_status(self, *, force: bool = False) -> None:
        if self._gmail_rest_probe_busy:
            return
        if (
            not force
            and isinstance(self._gmail_rest_status, dict)
            and self._gmail_rest_status.get("status") == "connecting"
        ):
            return
        now = datetime.now()
        if (
            not force
            and now - self._gmail_rest_last_probe < timedelta(seconds=45)
        ):
            return
        self._gmail_rest_probe_busy = True
        self._gmail_rest_last_probe = now

        def _bg():
            try:
                from osia.tools.gmail import gmail_connection_status
                status = gmail_connection_status(validate=True)
            except Exception as exc:
                status = {
                    "status": "failed",
                    "connected": False,
                    "emailAddress": None,
                    "error": f"Gmail status check failed: {type(exc).__name__}",
                }
            self._gmail_rest_status_ready.emit(status)

        threading.Thread(
            target=_bg, name="osia-gmail-rest-status", daemon=True
        ).start()

    def _apply_gmail_rest_status(self, status: dict) -> None:
        self._gmail_rest_probe_busy = False
        if not isinstance(status, dict):
            status = {
                "status": "failed",
                "connected": False,
                "emailAddress": None,
                "error": "Gmail status check failed.",
            }
        clean_status = dict(status)
        oauth_done = bool(clean_status.pop("_oauthFlowDone", False))
        if oauth_done:
            self._gmail_rest_oauth_busy = False
        self._gmail_rest_status = clean_status
        self._update_gmail_rest_widgets()

    @staticmethod
    def _gmail_rest_status_after_oauth() -> dict:
        """Validate Gmail status after OAuth, with short propagation retries."""
        import time
        from osia.tools.gmail import gmail_connection_status

        last_status: dict = {}
        for delay_s in (0.0, 1.0, 2.0):
            if delay_s:
                time.sleep(delay_s)
            status = gmail_connection_status(validate=True)
            if isinstance(status, dict):
                last_status = status
                if status.get("connected"):
                    return status
        return last_status

    def _update_gmail_rest_widgets(self) -> None:
        status = self._gmail_rest_status if isinstance(
            self._gmail_rest_status, dict
        ) else {}
        state = str(status.get("status") or "disconnected")
        if state not in self._MCP_PILL_COLORS:
            state = "disconnected"
        pill = self._mcp_pills.get("gmail")
        if pill is not None:
            self._set_mcp_pill(pill, state)
        btn = self._mcp_buttons.get("gmail")
        if btn is not None:
            btn.setText(
                i18n.t("mcp.disconnect")
                if state == "connected"
                else (
                    i18n.t("mcp.status_connecting")
                    if state == "connecting"
                    else i18n.t("mcp.gmail_connect")
                )
            )
            btn.setEnabled(state != "connecting" and not self._gmail_rest_oauth_busy)
            error = str(status.get("error") or "").strip()
            btn.setToolTip(error if state == "failed" else "")
        self._update_gmail_rest_account_label()
        self._update_gmail_rest_capability_label()
        self._update_gmail_rest_upgrade_button()

    def _update_gmail_rest_account_label(self) -> None:
        label = self._gmail_rest_account_labels.get("gmail")
        if label is None:
            return
        status = self._gmail_rest_status if isinstance(
            self._gmail_rest_status, dict
        ) else {}
        state = str(status.get("status") or "disconnected")
        email = str(status.get("emailAddress") or "").strip()
        error = str(status.get("error") or "").strip()
        if state == "connected" and email:
            label.setText(i18n.t("mcp.gmail_account", email=email))
        elif state == "failed" and error:
            label.setText(i18n.t("mcp.gmail_failed", error=error))
        else:
            label.setText(i18n.t("mcp.gmail_hint"))

    def _update_gmail_rest_capability_label(self) -> None:
        label = self._gmail_rest_capability_labels.get("gmail")
        if label is None:
            return
        status = self._gmail_rest_status if isinstance(
            self._gmail_rest_status, dict
        ) else {}
        state = str(status.get("status") or "disconnected")
        capabilities = status.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        compose_enabled = bool(
            capabilities.get("compose") or status.get("composeEnabled")
        )
        modify_enabled = bool(
            capabilities.get("modify") or status.get("modifyEnabled")
        )
        if state == "connected" and modify_enabled:
            label.setText(i18n.t("mcp.gmail_cap_modify"))
        elif state == "connected" and compose_enabled:
            label.setText(i18n.t("mcp.gmail_cap_compose"))
        elif state == "connected":
            label.setText(i18n.t("mcp.gmail_cap_readonly"))
        else:
            label.setText(i18n.t("mcp.gmail_cap_disconnected"))

        badge = self._gmail_rest_capability_badges.get("gmail")
        if badge is not None:
            if state == "connected" and modify_enabled:
                badge.setText(i18n.t("mcp.badge_modify"))
            elif state == "connected" and compose_enabled:
                badge.setText(i18n.t("mcp.badge_compose"))
            else:
                badge.setText(i18n.t("mcp.badge_readonly"))

    def _update_gmail_rest_upgrade_button(self) -> None:
        btn = self._gmail_rest_upgrade_buttons.get("gmail")
        if btn is None:
            return
        info_badge = self._gmail_rest_upgrade_info_badges.get("gmail")
        status = self._gmail_rest_status if isinstance(
            self._gmail_rest_status, dict
        ) else {}
        state = str(status.get("status") or "disconnected")
        capabilities = status.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        compose_required = bool(
            capabilities.get("composeUpgradeRequired")
            or status.get("composeUpgradeRequired")
        )
        modify_required = bool(
            capabilities.get("modifyUpgradeRequired")
            or status.get("modifyUpgradeRequired")
        )
        show_compose = state == "connected" and compose_required
        show_modify = state == "connected" and modify_required
        show = show_compose or show_modify
        btn.setText(i18n.t("mcp.gmail_upgrade"))
        btn.setToolTip(i18n.t("mcp.gmail_upgrade_hint") if show else "")
        btn.setVisible(show)
        btn.setEnabled(show and not self._gmail_rest_oauth_busy)
        if info_badge is not None:
            info_badge.setVisible(show)

    def _on_gmail_rest_toggle(self, entry: dict) -> None:
        if not self._is_gmail_rest_entry(entry):
            return
        status = self._gmail_rest_status if isinstance(
            self._gmail_rest_status, dict
        ) else {}
        if self._gmail_rest_oauth_busy or status.get("status") == "connecting":
            return
        if status.get("status") == "connected":
            self._on_gmail_rest_disconnect()
        else:
            self._on_gmail_rest_connect()

    def _on_gmail_rest_upgrade_permissions(self, _checked: bool = False) -> None:
        if self._gmail_rest_oauth_busy:
            return
        try:
            self._save_gmail_rest_client_from_fields()
        except ValueError:
            self._apply_gmail_rest_status({
                "status": "failed",
                "connected": False,
                "emailAddress": None,
                "error": i18n.t("mcp.gmail_client_required"),
            })
            return
        self._gmail_rest_oauth_busy = True
        self._apply_gmail_rest_status({
            "status": "connecting",
            "connected": False,
            "emailAddress": None,
            "error": "",
        })

        def _bg():
            try:
                from osia.tools.gmail import (
                    gmail_upgrade_modify_permissions,
                )
                gmail_upgrade_modify_permissions()
                status = self._gmail_rest_status_after_oauth()
                caps = status.get("capabilities")
                caps = caps if isinstance(caps, dict) else {}
                has_modify = bool(caps.get("modify") or status.get("modifyEnabled"))
                if not status.get("connected") or not has_modify:
                    status = {
                        "status": "failed",
                        "connected": False,
                        "emailAddress": None,
                        "error": i18n.t("mcp.gmail_upgrade_failed"),
                    }
            except Exception:
                try:
                    status = self._gmail_rest_status_after_oauth()
                except Exception:
                    status = {}
                caps = status.get("capabilities") if isinstance(status, dict) else {}
                caps = caps if isinstance(caps, dict) else {}
                has_modify = bool(caps.get("modify") or status.get("modifyEnabled"))
                if (
                    not isinstance(status, dict)
                    or not status.get("connected")
                    or not has_modify
                ):
                    status = {
                        "status": "failed",
                        "connected": False,
                        "emailAddress": None,
                        "error": i18n.t("mcp.gmail_upgrade_failed"),
                    }
            if not isinstance(status, dict):
                status = {}
            status["_oauthFlowDone"] = True
            self._gmail_rest_status_ready.emit(status)

        threading.Thread(
            target=_bg,
            name="osia-gmail-rest-elevate",
            daemon=True,
        ).start()

    def _on_gmail_rest_connect(self) -> None:
        if self._gmail_rest_oauth_busy:
            return
        try:
            self._save_gmail_rest_client_from_fields()
        except ValueError:
            self._apply_gmail_rest_status({
                "status": "failed",
                "connected": False,
                "emailAddress": None,
                "error": i18n.t("mcp.gmail_client_required"),
            })
            return
        self._gmail_rest_oauth_busy = True
        self._apply_gmail_rest_status({
            "status": "connecting",
            "connected": False,
            "emailAddress": None,
            "error": "",
        })

        def _bg():
            try:
                from osia.tools.gmail import (
                    gmail_connect,
                )
                gmail_connect()
                status = self._gmail_rest_status_after_oauth()
                if not status.get("connected") and not status.get("error"):
                    status = {
                        "status": "failed",
                        "connected": False,
                        "emailAddress": None,
                        "error": i18n.t("mcp.gmail_oauth_failed"),
                    }
            except Exception:
                status = {
                    "status": "failed",
                    "connected": False,
                    "emailAddress": None,
                    "error": i18n.t("mcp.gmail_oauth_failed"),
                }
            if not isinstance(status, dict):
                status = {}
            status["_oauthFlowDone"] = True
            self._gmail_rest_status_ready.emit(status)

        threading.Thread(
            target=_bg, name="osia-gmail-rest-connect", daemon=True
        ).start()

    def _on_gmail_rest_disconnect(self) -> None:
        self._apply_gmail_rest_status({
            "status": "connecting",
            "connected": False,
            "emailAddress": None,
            "error": "",
        })

        def _bg():
            try:
                from osia.tools.gmail import gmail_disconnect
                gmail_disconnect()
                status = {
                    "status": "disconnected",
                    "connected": False,
                    "emailAddress": None,
                    "error": "",
                }
            except Exception:
                status = {
                    "status": "failed",
                    "connected": False,
                    "emailAddress": None,
                    "error": i18n.t("mcp.gmail_disconnect_failed"),
                }
            self._gmail_rest_status_ready.emit(status)

        threading.Thread(
            target=_bg, name="osia-gmail-rest-disconnect", daemon=True
        ).start()

    def _on_mcp_toggle(self, entry: dict) -> None:
        sid = entry["id"]
        try:
            from osia.mcp_client import get_manager
            status = get_manager().status(sid)
        except Exception:
            status = "disconnected"
        if status == "connected":
            self._on_mcp_disconnect(sid)
        else:
            self._on_mcp_connect(entry)

    def _on_mcp_connect(self, entry: dict) -> None:
        connect_cfg, persist_cfg, token, client_id, client_secret = self._mcp_build_connect_cfg(entry)
        if connect_cfg is None:
            return
        sid = entry["id"]
        self._mcp_upsert_server(persist_cfg)
        # The token field is AUTHORITATIVE: write it unconditionally so blanking
        # the field and reconnecting actually CLEARS the stored secret (empty
        # string clears it via set_mcp_token). Only for connectable token cards
        # (which is where the field is rendered).
        if entry.get("auth_type") == "token" and sid in self._mcp_token_inputs:
            set_mcp_token(sid, token)
        # WOR-100: persist the user's non-DCR OAuth client_id into the gitignored
        # oauth_tokens.json (under the per-server ``static_client`` blob) — NEVER
        # mcp_servers. Authoritative from the field (re-entering changes it). The
        # connect gate (manager.connect) reads it, and build_oauth_provider re-seeds
        # the SDK ``client`` blob from it so DCR is skipped. Only touches
        # ``static_client``; the SDK ``token``/``client`` sub-blobs are preserved.
        if entry.get("oauth_no_dcr") and sid in self._mcp_client_id_inputs:
            blob = get_mcp_oauth(sid)
            if not isinstance(blob, dict):
                blob = {}
            sc = blob.get("static_client")
            sc = dict(sc) if isinstance(sc, dict) else {}
            sc["client_id"] = client_id
            # WOR-100 Phase 2: the client_secret rides the SAME gitignored
            # static_client blob (never mcp_servers). AUTHORITATIVE from the masked
            # field — a non-empty value is stored; blanking it CLEARS the stored
            # secret (a public-PKCE ``none`` client keeps none).
            if client_secret:
                sc["client_secret"] = client_secret
            else:
                sc.pop("client_secret", None)
            blob["static_client"] = sc
            set_mcp_oauth(sid, blob)
        # Optimistic, immediate feedback; the timer settles it on the real status
        # (~60 ticks ≈ 72s cap, comfortably past the manager's ~45s connect
        # timeout, after which status resolves to connected/failed).
        self._mcp_pending[sid] = ("connecting", {"connected", "failed"}, 50)
        if sid in self._mcp_pills:
            self._set_mcp_pill(self._mcp_pills[sid], "connecting")

        def _bg():
            status = "failed"
            try:
                from osia.mcp_client import get_manager
                status = get_manager().connect(connect_cfg)
            except Exception:
                pass
            # Phase 1 (direction A): mirror a successful connect to the mobile WS
            # server so connected phones repaint to the true PC state. Fired from
            # THIS background thread (never the Qt main thread), AFTER the manager
            # resolves — for OAuth the SDK has already persisted the fresh token to
            # disk by the time connect() returns CONNECTED, so the server
            # reconnects that id headlessly (no browser). Gated on a real
            # "connected" so a failed handshake never tells mobile "connected".
            if status == "connected":
                try:
                    from osia.main import _notify_mobile_mcp_sync
                    _notify_mobile_mcp_sync(sid, "connect")
                except Exception:
                    pass
        threading.Thread(target=_bg, name="osia-mcp-connect", daemon=True).start()

    def _on_mcp_disconnect(self, server_id: str) -> None:
        self._mcp_set_enabled(server_id, False)
        # Overwrites any prior (connect) pending for this id — the latest user
        # action wins; the disconnect bg thread drives the manager to the
        # terminal state the resolve set waits for.
        self._mcp_pending[server_id] = ("disconnected", {"disconnected", "failed"}, 25)
        if server_id in self._mcp_pills:
            self._set_mcp_pill(self._mcp_pills[server_id], "disconnected")

        def _bg():
            try:
                from osia.mcp_client import get_manager
                get_manager().disconnect(server_id)
            except Exception:
                pass
            # Phase 1 (direction A): mirror the disconnect to the mobile WS server
            # so phones repaint. Unconditional — disconnect is idempotent on the
            # server (a no-op when it never had this id connected). Best-effort,
            # silent no-op when the server is down. From this bg thread only.
            try:
                from osia.main import _notify_mobile_mcp_sync
                _notify_mobile_mcp_sync(server_id, "disconnect")
            except Exception:
                pass
        threading.Thread(target=_bg, name="osia-mcp-disconnect", daemon=True).start()

    def _on_mcp_cancel(self, entry: dict) -> None:
        """WOR-97: abort an in-flight OAuth login. The Cancel control is visible
        only while the pill is "connecting" (see _refresh_mcp_status), and only
        for OAuth connectors. A LOOPBACK (PC-initiated) login runs in THIS
        desktop manager → cancel it directly; a TUNNEL/PART-1 flow runs in the
        SERVER process (the desktop manager never had it CONNECTING) → bridge to
        the server's loopback /internal/mcp_oauth_cancel. Branch is chosen by the
        desktop manager's own status. Runs on a bg thread so the cancel (bounded
        ≤10s) never blocks the Qt UI thread."""
        sid = entry["id"]
        # Optimistic feedback (mirrors _on_mcp_disconnect): flip the pill toward
        # disconnected; the ~1.2s poll settles it on the real manager status. Hide
        # the Cancel button at once so a second click can't double-fire (harmless
        # anyway — cancel_pending / the route are both idempotent).
        self._mcp_pending[sid] = ("disconnected", {"disconnected", "failed"}, 25)
        if sid in self._mcp_pills:
            self._set_mcp_pill(self._mcp_pills[sid], "disconnected")
        if sid in self._mcp_cancel_buttons:
            self._mcp_cancel_buttons[sid].setVisible(False)

        def _bg():
            try:
                from osia.mcp_client import get_manager
                mgr = get_manager()
                # The desktop manager owns a loopback (PC-initiated) login → cancel
                # it directly. Otherwise it's a server-process tunnel flow → POST
                # the loopback bridge so the server cancels it.
                if mgr.status(sid) == "connecting":
                    mgr.cancel_pending(sid)
                else:
                    from osia.main import request_oauth_cancel
                    request_oauth_cancel(sid)
            except Exception:
                pass
        threading.Thread(
            target=_bg, name="osia-mcp-oauth-cancel", daemon=True
        ).start()

    def _on_mcp_switch_account(self, entry: dict) -> None:
        """WOR-98: switch the account on a CONNECTED OAuth connector. The control
        is visible only while the pill is "connected" (see _refresh_mcp_status) and
        only for OAuth connectors. Unlike a plain Disconnect (which KEEPS the token
        for a silent reconnect), this REVOKES the grant (RFC 7009) AND forgets the
        DCR client so the next Connect re-runs DCR + a fresh login — a clean slate
        for a different account. Does NOT auto-reconnect.

        Mirrors _on_mcp_disconnect/_on_mcp_cancel: persist enabled=False so the
        reconcile daemon won't reconnect, flip the pill + hide the button on the Qt
        UI thread, then do the bounded network work on a daemon bg thread. The
        revoke (RFC 7009) + forget (delete_mcp_oauth) are done IN-PROCESS here — like
        _on_mcp_disconnect / _on_mcp_remove_custom keep their core teardown local —
        so the clean slate holds even when the mobile server process is down. The bg
        thread then ALSO posts the /internal/mcp_oauth_switch bridge (best-effort) so
        the server tears down its OWN mirrored/tunnel session and repaints connected
        phones; that POST is now redundant for the revoke+forget (the shared
        oauth_tokens.json is already cleared), so a down server no longer defeats the
        feature. disconnect() is idempotent (a no-op when the manager doesn't own the
        id)."""
        sid = entry["id"]
        # Authoritative intent: enabled=False so the ~3s desktop reconcile daemon
        # won't reconnect this id after the teardown (mirrors _on_mcp_disconnect).
        self._mcp_set_enabled(sid, False)
        # Optimistic feedback: flip the pill toward disconnected (the ~1.2s poll
        # settles it on the real manager status) and hide the button at once so a
        # second click can't double-fire (harmless anyway — the route is idempotent).
        self._mcp_pending[sid] = ("disconnected", {"disconnected", "failed"}, 25)
        if sid in self._mcp_pills:
            self._set_mcp_pill(self._mcp_pills[sid], "disconnected")
        if sid in self._mcp_switch_buttons:
            self._mcp_switch_buttons[sid].setVisible(False)

        url = str(entry.get("url") or "").strip()

        def _bg():
            # Drop the desktop manager's own (loopback) session first; idempotent
            # no-op for a tunnel connector the desktop manager never owned.
            try:
                from osia.mcp_client import get_manager
                get_manager().disconnect(sid)
            except Exception:
                pass
            # AUTHORITATIVE revoke + forget done IN-PROCESS (mirrors how
            # _on_mcp_disconnect / _on_mcp_remove_custom keep their core teardown
            # local): the mobile server (:8975/:8765) is a SEPARATE process and may
            # be down while this GUI is up, so doing the clean-slate here guarantees
            # the next Connect re-runs DCR + a fresh login regardless. Order is
            # critical — revoke reads the token BEFORE delete_mcp_oauth removes the
            # whole blob (token + DCR client). Both best-effort; revoke_oauth_token
            # never raises and never logs token values. delete_mcp_oauth /
            # oauth_tokens.json are shared with the server via CONFIG_DIR, so the
            # server's mirror below sees the deletion (its own revoke/delete then
            # no-op, harmlessly idempotent).
            try:
                import asyncio
                from osia.mcp_client.oauth import revoke_oauth_token
                asyncio.run(revoke_oauth_token(sid, url))
            except Exception:
                pass
            try:
                delete_mcp_oauth(sid)
            except Exception:
                pass
            # Mirror to the server (best-effort): tear down ITS own (mirrored or
            # tunnel) session for this id and repaint every connected phone to
            # disconnected. The revoke+forget above already secured the clean slate,
            # so a server that is down no longer defeats the feature.
            try:
                from osia.main import request_oauth_switch
                request_oauth_switch(sid)
            except Exception:
                pass
        threading.Thread(
            target=_bg, name="osia-mcp-oauth-switch", daemon=True
        ).start()

    def _on_mcp_remove_custom(self, server_id: str) -> None:
        self._on_mcp_disconnect(server_id)
        self._mcp_remove_saved(server_id)
        try:
            delete_mcp_token(server_id)
            # WOR-44 Phase 5 (5-oauth-a): also drop any stored OAuth blob so a
            # removed server leaves no orphaned secret behind (mirrors the token
            # cleanup; a plain disconnect intentionally KEEPS the blob for silent
            # reconnect — only an explicit removal clears it).
            delete_mcp_oauth(server_id)
        except Exception:
            pass
        self._mcp_pending.pop(server_id, None)
        self._render_mcp()

    def _on_mcp_add_custom(self) -> None:
        dlg = _McpCustomDialog(self, anchor=self._mcp_add_btn)
        if not dlg.exec() or not dlg.result_cfg:
            return
        cfg = dlg.result_cfg
        # Ensure a unique id across catalog + saved servers.
        taken = {e["id"] for e in self._MCP_CATALOG} | {
            s["id"] for s in self._mcp_saved_servers()
        }
        base_id, sid, n = cfg["id"], cfg["id"], 2
        while sid in taken:
            sid = f"{base_id}-{n}"
            n += 1
        cfg["id"] = sid
        self._mcp_upsert_server(cfg)            # NOT connected — explicit action
        if dlg.result_token:
            set_mcp_token(sid, dlg.result_token)
        # Surface the new server straight away: select it so the dropdown lands
        # on its panel after _render_mcp rebuilds the item list.
        self._mcp_selected_id = sid
        self._render_mcp()

    def _refresh_mcp_status(self) -> None:
        if not getattr(self, "_mcp_pills", None):
            return
        try:
            from osia.mcp_client import get_manager
            snap = get_manager().servers()
        except Exception:
            snap = {}
        for sid, pill in self._mcp_pills.items():
            entry = self._mcp_entries.get(sid) or {}
            if self._is_gmail_rest_entry(entry):
                self._refresh_gmail_rest_status()
                self._update_gmail_rest_widgets()
                continue
            info = snap.get(sid, {}) if isinstance(snap, dict) else {}
            ms = info.get("status", "disconnected")
            err = info.get("error", "")
            pend = self._mcp_pending.get(sid)
            if pend:
                visual, resolve, ticks = pend
                ticks -= 1
                if ms in resolve or ticks <= 0:
                    self._mcp_pending.pop(sid, None)
                    state = ms
                else:
                    self._mcp_pending[sid] = (visual, resolve, ticks)
                    state = visual
            else:
                state = ms
            self._set_mcp_pill(pill, state)
            btn = self._mcp_buttons.get(sid)
            if btn is not None:
                # Keep the OAuth-specific connect label when disconnected (the
                # card built it as "Connect with OAuth"); the poll would
                # otherwise revert it to the generic "Connect".
                connect_label = (
                    i18n.t("mcp.connect_oauth")
                    if entry.get("auth_type") == "oauth"
                    else i18n.t("mcp.connect")
                )
                btn.setText(
                    i18n.t("mcp.disconnect") if state == "connected"
                    else connect_label
                )
                btn.setToolTip(err if (state == "failed" and err) else "")
            # WOR-97: surface the OAuth Cancel control ONLY while connecting.
            cancel_btn = self._mcp_cancel_buttons.get(sid)
            if cancel_btn is not None:
                cancel_btn.setVisible(state == "connecting")
            # WOR-98: surface the OAuth "Switch account" control ONLY while
            # connected (mutually exclusive with Cancel, which is connecting-only).
            switch_btn = self._mcp_switch_buttons.get(sid)
            if switch_btn is not None:
                switch_btn.setVisible(state == "connected")

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
        self._apikeys_desc = QLabel(i18n.t("settings.api_keys_desc"))
        self._apikeys_desc.setObjectName("sectionDesc")
        self._apikeys_desc.setWordWrap(True)
        l.addWidget(self._apikeys_desc)

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

    def _build_legal_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self._legal_title = QLabel(i18n.t("settings.legal"))
        self._legal_title.setObjectName("sectionTitle")
        self._legal_title.setWordWrap(True)
        self._legal_sub = QLabel(i18n.t("settings.legal_sub"))
        self._legal_sub.setObjectName("sectionSub")
        self._legal_sub.setWordWrap(True)
        self._legal_desc = QLabel(i18n.t("settings.legal_desc"))
        self._legal_desc.setObjectName("sectionDesc")
        self._legal_desc.setWordWrap(True)
        self._legal_status = QLabel(i18n.t("legal.review_status"))
        self._legal_status.setObjectName("warnLabel")
        self._legal_status.setWordWrap(True)
        self._legal_terms_version = QLabel(
            i18n.t("legal.terms_version", version=legal.TERMS_VERSION)
        )
        self._legal_terms_version.setWordWrap(True)
        self._legal_privacy_version = QLabel(
            i18n.t("legal.privacy_version", version=legal.PRIVACY_VERSION)
        )
        self._legal_privacy_version.setWordWrap(True)
        self._legal_effective_year = QLabel(
            i18n.t("legal.effective_year", year=legal.PLANNED_EFFECTIVE_YEAR)
        )
        self._legal_effective_year.setWordWrap(True)
        self._legal_contact = QLabel(
            i18n.t("legal.contact", email=legal.PUBLIC_CONTACT_EMAIL)
        )
        self._legal_contact.setWordWrap(True)
        self._legal_governing_law = QLabel(i18n.t("legal.governing_law"))
        self._legal_governing_law.setWordWrap(True)
        layout.addWidget(self._legal_title)
        layout.addWidget(self._legal_sub)
        layout.addWidget(self._legal_desc)
        layout.addWidget(self._legal_status)
        layout.addWidget(self._legal_terms_version)
        layout.addWidget(self._legal_privacy_version)
        layout.addWidget(self._legal_effective_year)
        layout.addWidget(self._legal_contact)
        layout.addWidget(self._legal_governing_law)

        self._legal_terms_btn = QPushButton(i18n.t("legal.open_terms"))
        self._legal_terms_btn.setObjectName("ghostBtn")
        self._legal_terms_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._legal_terms_btn.clicked.connect(
            lambda: webbrowser.open(legal.TERMS_URL)
        )
        self._legal_privacy_btn = QPushButton(i18n.t("legal.open_privacy"))
        self._legal_privacy_btn.setObjectName("ghostBtn")
        self._legal_privacy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._legal_privacy_btn.clicked.connect(
            lambda: webbrowser.open(legal.PRIVACY_URL)
        )
        self._legal_manifest_btn = QPushButton(i18n.t("legal.open_manifest"))
        self._legal_manifest_btn.setObjectName("ghostBtn")
        self._legal_manifest_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._legal_manifest_btn.clicked.connect(
            lambda: webbrowser.open(legal.LEGAL_MANIFEST_URL)
        )
        layout.addWidget(self._legal_terms_btn)
        layout.addWidget(self._legal_privacy_btn)
        layout.addWidget(self._legal_manifest_btn)
        return wrap

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
        self._mobile_desc = QLabel(i18n.t("settings.mobile_desc"))
        self._mobile_desc.setObjectName("sectionDesc")
        self._mobile_desc.setWordWrap(True)
        l.addWidget(self._mobile_desc)

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
        self._mobile_qr_btn = QPushButton(i18n.t("settings.show_qr"))
        self._mobile_qr_btn.setObjectName("ghostBtn")
        self._mobile_qr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mobile_qr_btn.clicked.connect(self._on_show_qr)
        self._mobile_regen_btn = QPushButton(i18n.t("settings.regenerate"))
        self._mobile_regen_btn.setObjectName("ghostBtn")
        self._mobile_regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mobile_regen_btn.clicked.connect(self._on_regenerate_token)
        # WOR-48 P3 — the token line and its two action buttons used to share
        # one row whose minimum (~650px incl. the unbreakable 20-char monospace
        # token) clipped even the WIDE master-detail viewport. The buttons now
        # sit on their own line below. Same widgets, same handlers — layout only.
        token_row.addWidget(self._mobile_token_label)
        token_row.addWidget(self._token_mask_label, 1)
        l.addLayout(token_row)

        token_btn_row = QHBoxLayout()
        token_btn_row.setSpacing(8)
        token_btn_row.addWidget(self._mobile_show_btn)
        token_btn_row.addWidget(self._mobile_qr_btn)
        token_btn_row.addWidget(self._mobile_regen_btn)
        token_btn_row.addStretch(1)
        l.addLayout(token_btn_row)

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
        self._ai_desc = QLabel(i18n.t("settings.ai_providers_desc"))
        self._ai_desc.setObjectName("sectionDesc")
        self._ai_desc.setWordWrap(True)
        l.addWidget(self._ai_desc)

        # Compact "Choose provider" dropdown: this is settings navigation only.
        # The ChatGPT account is a distinct UI entry and remains independent of
        # the existing OpenAI API-key provider and the effective runtime route.
        sel_row = QHBoxLayout()
        self._ai_prov_label = QLabel(i18n.t("settings.choose_provider"))
        self._ai_prov_label.setObjectName("fieldLabel")
        self._ai_prov_label.setMinimumWidth(96)
        self._ai_prov_label.setWordWrap(True)
        self._ai_provider_combo = QComboBox()
        self._ai_provider_combo.setObjectName("voiceCombo")
        self._ai_provider_combo.addItem(
            i18n.t("chatgpt_account.title"), "chatgpt_account"
        )
        for provider in (*self._AI_PROVIDER_ORDER, "ollama"):
            self._ai_provider_combo.addItem(PROVIDERS[provider]["name"], provider)
        sel_row.addWidget(self._ai_prov_label)
        sel_row.addWidget(self._ai_provider_combo, 1)
        l.addLayout(sel_row)

        # Provider-style ChatGPT account surface. It is shown only when its
        # dedicated settings-navigation entry is selected.
        self._chatgpt_card = QFrame()
        self._chatgpt_card.setObjectName("providerCard")
        self._chatgpt_card.setProperty("settingsEntry", "chatgpt_account")
        account_l = QVBoxLayout(self._chatgpt_card)
        account_l.setContentsMargins(12, 10, 12, 10)
        account_l.setSpacing(7)

        account_head = QHBoxLayout()
        account_head.setSpacing(7)
        self._chatgpt_name = QLabel(i18n.t("chatgpt_account.title"))
        self._chatgpt_name.setObjectName("providerName")
        self._chatgpt_name.setWordWrap(True)
        self._chatgpt_status = QLabel(i18n.t("chatgpt_account.checking"))
        self._chatgpt_status.setObjectName("providerSub")
        self._chatgpt_status.setWordWrap(True)
        account_head.addWidget(self._chatgpt_name)
        account_head.addStretch(1)
        account_head.addWidget(self._chatgpt_status)
        self._chatgpt_info_btn = _FocusTooltipButton("i")
        self._chatgpt_info_btn.setObjectName("infoBadge")
        self._chatgpt_info_btn.setFixedSize(18, 18)
        self._chatgpt_info_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._chatgpt_info_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._set_chatgpt_info_text()
        account_head.addWidget(self._chatgpt_info_btn)
        account_l.addLayout(account_head)

        self._chatgpt_account_detail = QLabel("")
        self._chatgpt_account_detail.setObjectName("providerSub")
        self._chatgpt_account_detail.setWordWrap(True)
        self._chatgpt_account_detail.setVisible(False)
        account_l.addWidget(self._chatgpt_account_detail)

        self._chatgpt_usage = QLabel("")
        self._chatgpt_usage.setObjectName("providerSub")
        self._chatgpt_usage.setWordWrap(True)
        self._chatgpt_usage.setVisible(False)
        account_l.addWidget(self._chatgpt_usage)

        account_actions = QHBoxLayout()
        account_actions.setSpacing(7)
        self._chatgpt_connect_btn = QPushButton(i18n.t("chatgpt_account.connect"))
        self._chatgpt_connect_btn.setObjectName("ghostBtn")
        self._chatgpt_connect_btn.clicked.connect(self._on_chatgpt_connect)
        self._chatgpt_cancel_btn = QPushButton(i18n.t("chatgpt_account.cancel"))
        self._chatgpt_cancel_btn.setObjectName("ghostBtn")
        self._chatgpt_cancel_btn.clicked.connect(self._on_chatgpt_cancel)
        self._chatgpt_disconnect_btn = QPushButton(i18n.t("chatgpt_account.disconnect"))
        self._chatgpt_disconnect_btn.setObjectName("ghostBtn")
        self._chatgpt_disconnect_btn.clicked.connect(self._on_chatgpt_disconnect)
        self._chatgpt_retry_btn = QPushButton(i18n.t("chatgpt_account.retry"))
        self._chatgpt_retry_btn.setObjectName("ghostBtn")
        self._chatgpt_retry_btn.clicked.connect(self._on_chatgpt_retry)
        for button in (
            self._chatgpt_connect_btn, self._chatgpt_cancel_btn,
            self._chatgpt_disconnect_btn, self._chatgpt_retry_btn,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            account_actions.addWidget(button)
        account_actions.addStretch(1)
        account_l.addLayout(account_actions)
        l.addWidget(self._chatgpt_card)

        self._set_chatgpt_ui_state("checking")

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

        # ── Vision companion model (WOR-25) ─────────────────────────────────
        # Shown only when the selected main model can't process images (e.g.
        # DeepSeek text): the user pairs it with a vision-capable model and that
        # provider's API key. Hidden entirely when the main model already sees.
        # Visibility + prefill are driven from _apply_vision_row() in
        # _load_state(), so the row tracks whichever model is currently selected.
        self._vision_models_by_provider: dict[str, list] = {}
        for m in vision_capable_models():
            self._vision_models_by_provider.setdefault(m["provider"], []).append(m)
        self._vision_provider_order = [
            p for p in self._AI_PROVIDER_ORDER
            if p in self._vision_models_by_provider
        ]

        self._vision_row = QFrame()
        self._vision_row.setObjectName("providerCard")
        self._vision_row.setVisible(False)
        vbox = QVBoxLayout(self._vision_row)
        vbox.setContentsMargins(12, 10, 12, 10)
        vbox.setSpacing(8)

        self._vision_heading = QLabel(i18n.t("settings.vision_model"))
        self._vision_heading.setObjectName("providerName")
        self._vision_heading.setWordWrap(True)
        vbox.addWidget(self._vision_heading)

        # Small italic hint: clarifies when pairing a vision model is useful
        # (shown above the provider dropdown). Style: see QLabel#visionHint.
        self._vision_hint = QLabel(i18n.t("settings.vision_hint"))
        self._vision_hint.setObjectName("visionHint")
        self._vision_hint.setWordWrap(True)
        vbox.addWidget(self._vision_hint)

        # Provider dropdown (vision-capable providers only).
        vprov_row = QHBoxLayout()
        vprov_row.setSpacing(8)
        self._vision_prov_label = QLabel(i18n.t("settings.vision_provider"))
        self._vision_prov_label.setObjectName("fieldLabel")
        self._vision_prov_label.setMinimumWidth(96)
        self._vision_prov_label.setWordWrap(True)
        self._vision_provider_combo = QComboBox()
        self._vision_provider_combo.setObjectName("voiceCombo")
        for p in self._vision_provider_order:
            self._vision_provider_combo.addItem(PROVIDERS[p]["name"], p)
        self._vision_provider_combo.currentIndexChanged.connect(
            self._on_vision_provider_changed
        )
        vprov_row.addWidget(self._vision_prov_label)
        vprov_row.addWidget(self._vision_provider_combo, 1)
        vbox.addLayout(vprov_row)

        # Model dropdown (repopulated per provider).
        vmodel_row = QHBoxLayout()
        vmodel_row.setSpacing(8)
        self._vision_model_label = QLabel(i18n.t("settings.model"))
        self._vision_model_label.setObjectName("fieldLabel")
        self._vision_model_label.setMinimumWidth(96)
        self._vision_model_label.setWordWrap(True)
        self._vision_model_combo = QComboBox()
        self._vision_model_combo.setObjectName("voiceCombo")
        self._vision_model_combo.currentIndexChanged.connect(
            self._on_vision_model_changed
        )
        vmodel_row.addWidget(self._vision_model_label)
        vmodel_row.addWidget(self._vision_model_combo, 1)
        vbox.addLayout(vmodel_row)

        # API key for the chosen vision provider — same storage as that
        # provider's main key (api_keys.json keyed by provider).
        vkey_row = QHBoxLayout()
        vkey_row.setSpacing(8)
        self._vision_key_dot = QLabel()
        self._vision_key_dot.setObjectName("statusDot")
        self._vision_key_dot.setWordWrap(True)
        self._vision_key_dot.setFixedSize(8, 8)
        self._vision_key_dot.setProperty("state", "stopped")
        self._vision_key_input = QLineEdit()
        self._vision_key_input.setObjectName("keyInput")
        self._vision_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._vision_key_save = QPushButton(i18n.t("provider.save"))
        self._vision_key_save.setObjectName("saveBtn")
        self._vision_key_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vision_key_save.clicked.connect(self._on_vision_key_save)
        vkey_row.addWidget(self._vision_key_dot)
        vkey_row.addWidget(self._vision_key_input, 1)
        vkey_row.addWidget(self._vision_key_save)
        vbox.addLayout(vkey_row)

        l.addWidget(self._vision_row)

        # Wire the selector and reveal the first provider's card.
        self._ai_provider_combo.currentIndexChanged.connect(
            self._on_ai_provider_changed
        )
        self._on_ai_provider_changed(self._ai_provider_combo.currentIndex())

        return wrap

    def _set_chatgpt_ui_state(self, state: str) -> None:
        connecting = state in {"connecting", "waiting"}
        connected = state == "connected"
        checking = state == "checking"
        self._chatgpt_status.setVisible(state != "disconnected")
        if not connected:
            self._chatgpt_account_detail.setVisible(False)
            self._chatgpt_usage.setVisible(False)
        self._chatgpt_connect_btn.setVisible(state == "disconnected")
        self._chatgpt_cancel_btn.setVisible(connecting)
        self._chatgpt_disconnect_btn.setVisible(connected)
        self._chatgpt_retry_btn.setVisible(state == "error")
        if checking:
            self._chatgpt_status.setText(i18n.t("chatgpt_account.checking"))
        elif state == "connecting":
            self._chatgpt_status.setText(i18n.t("chatgpt_account.connecting"))
        elif state == "waiting":
            self._chatgpt_status.setText(i18n.t("chatgpt_account.waiting"))
        elif connected:
            self._chatgpt_status.setText("✓ " + i18n.t("chatgpt_account.connected"))
        elif state == "disconnected":
            self._chatgpt_status.setText(i18n.t("chatgpt_account.disconnected"))
        for button in (
            self._chatgpt_connect_btn, self._chatgpt_disconnect_btn,
            self._chatgpt_retry_btn,
        ):
            button.setEnabled(not self._chatgpt_action_busy and not checking)

    def _set_chatgpt_info_text(self) -> None:
        tooltip = i18n.t("chatgpt_account.info_tooltip")
        self._chatgpt_info_btn.setToolTip(tooltip)
        self._chatgpt_info_btn.setAccessibleName(
            i18n.t("chatgpt_account.info_accessible")
        )
        self._chatgpt_info_btn.setAccessibleDescription(tooltip)

    @staticmethod
    def _chatgpt_status_with_usage(provider) -> dict:
        status = provider.status(refresh_models=True)
        if not status.get("connected"):
            return status
        selected = str(status.get("selectedModel") or "").strip()
        if selected and str(get_setting("chatgpt_account_model", "") or "") != selected:
            # Persist the provider's deterministic default/first valid model when
            # a saved account model disappeared. This is account-only state and
            # never touches the manual/API selected_model field.
            try:
                provider.set_selected_model(selected)
            except Exception:
                # The catalog may have changed between status() and this write.
                # Keep the valid snapshot visible; the next async refresh retries.
                pass
        try:
            payload = provider.rate_limits()
            from osia.chatgpt_account.rate_limits import quota_windows

            status = dict(status)
            status["quotaWindows"] = quota_windows(payload)
        except Exception:
            pass
        return status

    def _refresh_chatgpt_usage_text(self) -> None:
        status = self._chatgpt_last_status or {}
        parts = []
        for window in status.get("quotaWindows") or []:
            if not isinstance(window, dict):
                continue
            label = str(window.get("label") or "").strip()
            remaining = window.get("remainingPercent")
            if (
                not label
                or not isinstance(remaining, int)
                or isinstance(remaining, bool)
                or not 0 <= remaining <= 100
            ):
                continue
            if label.endswith("Weekly"):
                label = label[:-len("Weekly")] + i18n.t(
                    "chatgpt_account.quota_weekly"
                )
            parts.append(
                i18n.t(
                    "chatgpt_account.quota_remaining",
                    label=label,
                    percent=remaining,
                )
            )
        text = " · ".join(parts)
        self._chatgpt_usage.setText(text)
        self._chatgpt_usage.setVisible(bool(text and status.get("connected")))

    def _probe_chatgpt_account(self, force: bool = False) -> None:
        now = datetime.now()
        if self._chatgpt_action_busy:
            if force:
                self._chatgpt_refresh_pending = True
            return
        if (
            not force
            and self._chatgpt_last_status is not None
            and now - self._chatgpt_last_probe < self._CHATGPT_STATUS_TTL
        ):
            return
        if self._chatgpt_probe_busy:
            if force:
                self._chatgpt_request_generation += 1
                self._chatgpt_refresh_pending = True
            return

        self._chatgpt_probe_busy = True
        self._chatgpt_request_generation += 1
        generation = self._chatgpt_request_generation
        self._chatgpt_active_probe_generation = generation
        if self._chatgpt_last_status is None:
            self._set_chatgpt_ui_state("checking")

        def worker():
            try:
                from osia.chatgpt_account.provider import get_account_provider
                status = self._chatgpt_status_with_usage(get_account_provider())
            except Exception:
                status = {
                    "available": False, "runtimeAvailable": False,
                    "connected": False, "models": [], "error": "runtime_missing",
                }
            status = dict(status)
            status["_osiaRequestGeneration"] = generation
            status["_osiaRequestKind"] = "probe"
            try:
                self._chatgpt_status_ready.emit(status)
            except RuntimeError:
                # The Settings panel may have been destroyed during shutdown.
                pass

        threading.Thread(
            target=worker, name="osia-chatgpt-status", daemon=True
        ).start()

    def _restore_chatgpt_retry_view(self, *, clear: bool = False) -> None:
        state = self._chatgpt_retry_view_state
        if state is None:
            return
        row, scroll_value = state
        if self._settings_nav.currentRow() != row:
            self._settings_nav.setCurrentRow(row)
        self._settings_scroll.verticalScrollBar().setValue(scroll_value)
        if clear:
            self._chatgpt_retry_view_state = None

    def _configure_chatgpt_file_watcher(self) -> None:
        """Watch the shared Codex home for cross-process account changes."""
        try:
            from osia.chatgpt_account.provider import get_account_provider

            client = get_account_provider().client
            home = Path(client.home_dir)
            auth_file = home / "auth.json"
            paths = [path for path in (home, auth_file) if path.exists()]
            if not paths:
                parent = Path(client.base_dir)
                while not parent.exists() and parent != parent.parent:
                    parent = parent.parent
                if parent.exists():
                    paths = [parent]
            current = set(
                self._chatgpt_file_watcher.files()
                + self._chatgpt_file_watcher.directories()
            )
            wanted = {str(path) for path in paths}
            stale = list(current - wanted)
            if stale:
                self._chatgpt_file_watcher.removePaths(stale)
            missing = list(wanted - current)
            if missing:
                self._chatgpt_file_watcher.addPaths(missing)
        except Exception:
            pass

    def _on_chatgpt_auth_storage_changed(self, _path: str) -> None:
        self._configure_chatgpt_file_watcher()
        self._probe_chatgpt_account(force=True)

    def _apply_chatgpt_status(self, status: dict) -> None:
        status = dict(status or {})
        generation = status.pop("_osiaRequestGeneration", None)
        request_kind = status.pop("_osiaRequestKind", None)
        if generation is not None and generation != self._chatgpt_request_generation:
            if (
                request_kind == "probe"
                and generation == self._chatgpt_active_probe_generation
            ):
                self._chatgpt_probe_busy = False
                self._chatgpt_active_probe_generation = None
            if (
                self._chatgpt_refresh_pending
                and not self._chatgpt_probe_busy
                and not self._chatgpt_action_busy
            ):
                self._chatgpt_refresh_pending = False
                QTimer.singleShot(0, lambda: self._probe_chatgpt_account(force=True))
            return

        if request_kind == "probe":
            self._chatgpt_probe_busy = False
            self._chatgpt_active_probe_generation = None
        elif request_kind == "action":
            self._chatgpt_action_busy = False
        else:
            self._chatgpt_probe_busy = False
            self._chatgpt_action_busy = False
        self._chatgpt_refresh_pending = False
        self._chatgpt_last_probe = datetime.now()
        self._chatgpt_last_status = status
        self._refresh_chatgpt_usage_text()
        connected = bool(status.get("connected"))
        available = bool(status.get("available"))
        error = str(status.get("error") or "")

        if connected:
            account = status.get("account") if isinstance(status.get("account"), dict) else {}
            details = []
            if account.get("email"):
                details.append(str(account["email"]))
            if account.get("planType") and account.get("planType") != "unknown":
                details.append(str(account["planType"]).replace("_", " ").title())
            self._chatgpt_account_detail.setText(" · ".join(details))
            self._chatgpt_account_detail.setVisible(bool(details))
            if available:
                self._set_chatgpt_ui_state("connected")
            else:
                self._chatgpt_status.setText(i18n.t("chatgpt_account.error"))
                self._set_chatgpt_ui_state("error")
        elif error == "disconnected":
            self._chatgpt_account_detail.setVisible(False)
            self._set_chatgpt_ui_state("disconnected")
        else:
            self._chatgpt_account_detail.setVisible(False)
            if error == "runtime_missing":
                key = "chatgpt_account.runtime_missing"
            elif error == "runtime_incompatible":
                key = "chatgpt_account.runtime_incompatible"
            elif error == "isolation_failed":
                key = "chatgpt_account.isolation_error"
            elif error == "browser_failed":
                key = "chatgpt_account.browser_failed"
            elif error == "startup_failed":
                key = "chatgpt_account.runtime_start_failed"
            elif error in {"login_failed", "login_timeout", "login_invalid"}:
                key = "chatgpt_account.login_failed"
            else:
                key = "chatgpt_account.error"
            self._chatgpt_status.setText(i18n.t(key))
            self._set_chatgpt_ui_state("error")

        if hasattr(self, "_hier_values"):
            self._render_hier_category("heavy")
            self._render_hier_category("medium")
        self._restore_chatgpt_retry_view(clear=True)
        self.chatgpt_status_changed.emit(dict(self._chatgpt_last_status))

    def _on_chatgpt_connect(self) -> None:
        self._start_chatgpt_login(reset_stale=False)

    def _on_chatgpt_retry(self) -> None:
        if self._chatgpt_action_busy:
            return
        self._chatgpt_retry_view_state = (
            self._settings_nav.currentRow(),
            self._settings_scroll.verticalScrollBar().value(),
        )
        if self._chatgpt_retry_btn.hasFocus():
            self._chatgpt_info_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        self._start_chatgpt_login(reset_stale=True)

    def _start_chatgpt_login(self, *, reset_stale: bool) -> None:
        if self._chatgpt_action_busy:
            return
        self._chatgpt_action_busy = True
        self._chatgpt_last_probe = datetime.min
        self._chatgpt_request_generation += 1
        generation = self._chatgpt_request_generation
        self._set_chatgpt_ui_state("connecting")
        if self._chatgpt_retry_view_state is not None:
            QTimer.singleShot(0, self._restore_chatgpt_retry_view)

        def worker():
            try:
                from osia.chatgpt_account.provider import get_account_provider
                provider = get_account_provider()
                if reset_stale:
                    provider.reset_login()
                login = provider.begin_login()
                auth_url = str(login.get("authUrl") or "")
                try:
                    browser_opened = bool(auth_url and webbrowser.open(auth_url, new=2))
                except Exception:
                    browser_opened = False
                if not browser_opened:
                    try:
                        provider.cancel_login()
                    except Exception:
                        pass
                    raise RuntimeError("browser_failed")
                self._chatgpt_login_ready.emit({
                    "state": "waiting",
                    "_osiaRequestGeneration": generation,
                })
                provider.wait_for_login(login["loginId"], timeout=300.0)
                status = self._chatgpt_status_with_usage(provider)
            except Exception as exc:
                category = (
                    "browser_failed"
                    if str(exc) == "browser_failed"
                    else str(getattr(exc, "category", "login_failed"))
                )
                status = {
                    "available": False,
                    "runtimeAvailable": category not in {
                        "runtime_missing", "runtime_incompatible", "startup_failed",
                    },
                    "connected": False, "models": [],
                    "error": "disconnected" if category == "login_cancelled" else "login_failed",
                }
                if category != "login_cancelled":
                    status["error"] = category
            status = dict(status)
            status["_osiaRequestGeneration"] = generation
            status["_osiaRequestKind"] = "action"
            self._chatgpt_status_ready.emit(status)

        threading.Thread(
            target=worker, name="osia-chatgpt-login", daemon=True
        ).start()

    def _apply_chatgpt_login_action(self, action: dict) -> None:
        if (
            action.get("_osiaRequestGeneration") == self._chatgpt_request_generation
            and action.get("state") == "waiting"
            and self._chatgpt_action_busy
        ):
            self._set_chatgpt_ui_state("waiting")

    def _on_chatgpt_cancel(self) -> None:
        if not self._chatgpt_action_busy:
            return
        self._chatgpt_last_probe = datetime.min
        self._chatgpt_request_generation += 1
        generation = self._chatgpt_request_generation

        def worker():
            try:
                from osia.chatgpt_account.provider import get_account_provider
                get_account_provider().cancel_login()
            except Exception:
                pass
            self._chatgpt_status_ready.emit({
                "available": False, "runtimeAvailable": True,
                "connected": False, "models": [], "error": "disconnected",
                "_osiaRequestGeneration": generation,
                "_osiaRequestKind": "action",
            })

        threading.Thread(
            target=worker, name="osia-chatgpt-login-cancel", daemon=True
        ).start()

    def _on_chatgpt_disconnect(self) -> None:
        if self._chatgpt_action_busy:
            return
        self._chatgpt_action_busy = True
        self._chatgpt_last_probe = datetime.min
        self._chatgpt_request_generation += 1
        generation = self._chatgpt_request_generation
        self._set_chatgpt_ui_state("checking")

        def worker():
            try:
                from osia.chatgpt_account.provider import get_account_provider
                get_account_provider().logout()
            except Exception:
                pass
            self._chatgpt_status_ready.emit({
                "available": False, "runtimeAvailable": True,
                "connected": False, "models": [], "error": "disconnected",
                "_osiaRequestGeneration": generation,
                "_osiaRequestKind": "action",
            })

        threading.Thread(
            target=worker, name="osia-chatgpt-logout", daemon=True
        ).start()

    def _on_ai_provider_changed(self, _idx: int) -> None:
        provider = self._ai_provider_combo.currentData()
        self._chatgpt_card.setVisible(provider == "chatgpt_account")
        for p, card in self._provider_cards.items():
            card.setVisible(p == provider)
        self._ollama_row.setVisible(provider == "ollama")

    # ── Vision companion (WOR-25) ──────────────────────────────────────────

    def _apply_vision_row(self, settings: dict) -> None:
        """Show the vision-companion row only when the selected main model
        can't process images, and prefill it from ``settings`` (WOR-25)."""
        main_model = (settings.get("selected_model") or "").strip()
        main_provider = provider_for_model_cached(main_model) if main_model else None
        show = bool(main_model) and not model_has_vision(main_provider, main_model)
        self._vision_row.setVisible(show)
        if not show or not self._vision_provider_order:
            return
        v_provider = (settings.get("vision_provider") or "").strip()
        if v_provider not in self._vision_provider_order:
            v_provider = self._vision_provider_order[0]
        v_model = (settings.get("vision_model") or "").strip()
        self._vision_provider_combo.blockSignals(True)
        for i in range(self._vision_provider_combo.count()):
            if self._vision_provider_combo.itemData(i) == v_provider:
                self._vision_provider_combo.setCurrentIndex(i)
                break
        self._vision_provider_combo.blockSignals(False)
        self._populate_vision_models(v_provider, v_model)
        self._refresh_vision_key_field(v_provider)

    def _populate_vision_models(self, provider: str, model: str | None) -> None:
        """Fill the vision-model dropdown with ``provider``'s vision models and
        select ``model`` (or the first entry)."""
        combo = self._vision_model_combo
        combo.blockSignals(True)
        combo.clear()
        for m in self._vision_models_by_provider.get(provider, []):
            combo.addItem(m.get("label") or m["id"], m["id"])
        idx = 0
        if model:
            for i in range(combo.count()):
                if combo.itemData(i) == model:
                    idx = i
                    break
        if combo.count():
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _refresh_vision_key_field(self, provider: str) -> None:
        """Prefill the vision key field with ``provider``'s saved key and refresh
        the placeholder + status dot."""
        key = get_api_key(provider)
        self._vision_key_input.setText(key or "")
        self._vision_key_input.setPlaceholderText(
            i18n.t("provider.key_placeholder",
                   name=PROVIDERS.get(provider, {}).get("name", provider))
        )
        self._vision_key_dot.setProperty("state", "running" if key else "stopped")
        self._vision_key_dot.style().unpolish(self._vision_key_dot)
        self._vision_key_dot.style().polish(self._vision_key_dot)

    def _on_vision_provider_changed(self, _idx: int) -> None:
        provider = self._vision_provider_combo.currentData()
        if not provider:
            return
        set_setting("vision_provider", provider)
        models = self._vision_models_by_provider.get(provider, [])
        default_model = models[0]["id"] if models else ""
        set_setting("vision_model", default_model)
        self._populate_vision_models(provider, default_model)
        self._refresh_vision_key_field(provider)

    def _on_vision_model_changed(self, _idx: int) -> None:
        model = self._vision_model_combo.currentData()
        if model:
            set_setting("vision_model", model)

    def _on_vision_key_save(self) -> None:
        provider = self._vision_provider_combo.currentData()
        if not provider:
            return
        key = self._vision_key_input.text().strip()
        set_api_key(provider, key)
        # Keep the matching provider card (if built) in sync with the new key.
        card = self._provider_cards.get(provider)
        if card is not None:
            card.set_key(key)
        self._refresh_vision_key_field(provider)
        self._flash_vision_saved()

    def _flash_vision_saved(self) -> None:
        btn = self._vision_key_save
        btn.setText(i18n.t("provider.saved"))
        btn.setProperty("saved", True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

        def restore():
            btn.setProperty("saved", False)
            btn.setText(i18n.t("provider.save"))
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        QTimer.singleShot(1500, restore)

    # ── State load / save ─────────────────────────────────────────────────

    def _load_state(self) -> None:
        self._sync_all_cards_keys()
        self._sync_theme_buttons()

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

        # Acoustic wake is intentionally unavailable in this release.
        self._wake_word_toggle.blockSignals(True)
        self._wake_word_toggle.setChecked(False)
        self._wake_word_toggle.setEnabled(False)
        self._wake_word_toggle.blockSignals(False)

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
            self._LIVEAPI_AVAILABLE
            and bool(settings.get("liveapi_enabled", False))
        )
        self._liveapi_enabled_toggle.blockSignals(False)
        self._liveapi_enabled_toggle.setEnabled(self._LIVEAPI_AVAILABLE)
        self._apply_liveapi_visibility()

        # Vision companion (WOR-25) — show + prefill only when the selected
        # main model can't see; hidden otherwise.
        self._apply_vision_row(settings)

        # Scheduled tasks (Bloc 5) — rebuild the cron job editor.
        cron_jobs = settings.get("cron_jobs", []) or []
        self._cron_jobs = []
        for j in cron_jobs:
            if not isinstance(j, dict):
                continue
            entry = dict(j)
            entry.update({
                "task": str(j.get("task", "")),
                "schedule": str(j.get("schedule", "")),
                "enabled": bool(j.get("enabled", False)),
                "conversation_id": j.get("conversation_id") or None,
            })
            self._cron_jobs.append(entry)
        self._render_cron()

        # MCP / Connectors (WOR-44) — rebuild the catalog + custom server cards
        # from the saved list, then paint each pill with the manager's live
        # status (so a connection persists visually across reopen).
        self._render_mcp()
        self._refresh_memory_entries()

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
        self._state_loaded_at = datetime.now()

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
        # A provider change must not let an already-queued response continue
        # with a mixture of old/new voices.  Import lazily so GUI startup keeps
        # the existing lightweight path.
        from osia.voice.tts import stop as stop_tts

        stop_tts()
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
        # If the vision companion row is visible and pinned to the same provider,
        # mirror the newly-saved key there too (the vision->card direction is
        # already handled in _on_vision_key_save).
        if (not self._vision_row.isHidden()
                and self._vision_provider_combo.currentData() == _provider):
            self._refresh_vision_key_field(_provider)
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

    def _on_show_qr(self) -> None:
        self._mobile_token = load_mobile_token()
        self._update_token_mask()
        if not self._mobile_token:
            return
        dialog = _PairingQrDialog(
            self, self._mobile_token, anchor=self._mobile_qr_btn
        )
        dialog.exec()

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
        self._theme_title.setText(i18n.t("theme.section"))
        self._theme_sub.setText(i18n.t("theme.section_sub"))
        self._theme_desc.setText(i18n.t("theme.section_desc"))
        for key, button in self._theme_buttons.items():
            button.setText(i18n.t(PALETTES[key].name_key))

        self._capsule_title.setText(i18n.t("settings.capsule"))
        self._capsule_sub.setText(i18n.t("settings.capsule_sub"))
        self._capsule_desc.setText(i18n.t("settings.capsule_desc"))
        self._show_capsule_label.setText(i18n.t("settings.show_capsule"))
        self._autostart_label.setText(i18n.t("settings.autostart_gui"))
        self._restart_title.setText(i18n.t("settings.restart"))
        self._restart_sub.setText(i18n.t("settings.restart_sub"))
        self._restart_desc.setText(i18n.t("settings.restart_desc"))
        self._restart_btn.setText(i18n.t("settings.restart_button"))
        self._close_osia_btn.setText(i18n.t("settings.close_button"))
        self._close_osia_btn.setAccessibleName(self._close_osia_btn.text())
        if self._restart_status.isVisible() and self._power_status_key:
            self._restart_status.setText(i18n.t(self._power_status_key))
        self._guide_title.setText(i18n.t("settings.getting_started"))
        self._guide_sub.setText(i18n.t("settings.getting_started_sub"))
        self._guide_desc.setText(i18n.t("settings.getting_started_desc"))
        self._guide_btn.setText(i18n.t("settings.getting_started_button"))
        self._guide_btn.setAccessibleName(self._guide_btn.text())
        self._permissions_title.setText(i18n.t("permissions.settings_title"))
        self._permissions_sub.setText(i18n.t("permissions.settings_sub"))
        self._permissions_desc.setText(i18n.t("permissions.settings_desc"))
        self._permissions_page.retranslate()
        self._refresh_permissions_summary()

        self._ai_title.setText(i18n.t("settings.ai_providers"))
        self._ai_sub.setText(i18n.t("settings.ai_providers_sub"))
        self._ai_desc.setText(i18n.t("settings.ai_providers_desc"))
        self._ai_prov_label.setText(i18n.t("settings.choose_provider"))
        self._chatgpt_name.setText(i18n.t("chatgpt_account.title"))
        for index in range(self._ai_provider_combo.count()):
            if self._ai_provider_combo.itemData(index) == "chatgpt_account":
                self._ai_provider_combo.setItemText(
                    index, i18n.t("chatgpt_account.title")
                )
                break
        self._set_chatgpt_info_text()
        self._refresh_chatgpt_usage_text()
        self._chatgpt_connect_btn.setText(i18n.t("chatgpt_account.connect"))
        self._chatgpt_cancel_btn.setText(i18n.t("chatgpt_account.cancel"))
        self._chatgpt_disconnect_btn.setText(i18n.t("chatgpt_account.disconnect"))
        self._chatgpt_retry_btn.setText(i18n.t("chatgpt_account.retry"))

        # Vision companion (WOR-25)
        self._vision_heading.setText(i18n.t("settings.vision_model"))
        self._vision_hint.setText(i18n.t("settings.vision_hint"))
        self._vision_prov_label.setText(i18n.t("settings.vision_provider"))
        self._vision_model_label.setText(i18n.t("settings.model"))
        if not self._vision_key_save.property("saved"):
            self._vision_key_save.setText(i18n.t("provider.save"))
        _vprov = self._vision_provider_combo.currentData()
        if _vprov:
            self._vision_key_input.setPlaceholderText(
                i18n.t("provider.key_placeholder",
                       name=PROVIDERS.get(_vprov, {}).get("name", _vprov))
            )

        self._voice_title.setText(i18n.t("settings.voice_providers"))
        self._voice_sub.setText(i18n.t("settings.voice_providers_sub"))
        self._voice_desc.setText(i18n.t("settings.voice_providers_desc"))
        self._voice_prov_label.setText(i18n.t("settings.active_provider"))
        self._voice_voice_label.setText(i18n.t("settings.voice"))
        self._kokoro_tag.setText(i18n.t("tts.kokoro_desc"))
        self._tts_provider_combo.setItemText(0, i18n.t("tts.local_option"))

        self._wake_title.setText(i18n.t("settings.wake_greeting"))
        self._wake_sub.setText(i18n.t("settings.wake_greeting_sub"))
        self._wake_desc.setText(i18n.t("settings.wake_greeting_desc"))
        self._wake_word_label.setText(i18n.t("settings.wake_word_enabled"))
        self._wake_word_toggle.setToolTip(
            i18n.t("settings.wake_word_enabled")
        )
        self._wake_enabled_label.setText(i18n.t("wake.greeting_enabled"))
        self._wake_text_input.setPlaceholderText(i18n.t("wake.greeting_placeholder"))
        # If the greeting hasn't been customised, follow the UI language.
        if not (load_settings().get("wake_greeting_text") or "").strip():
            self._wake_text_input.setText(i18n.t("wake.greeting_default"))

        self._stt_title.setText(i18n.t("settings.whisper_providers"))
        self._stt_sub.setText(i18n.t("settings.whisper_providers_sub"))
        self._stt_desc.setText(i18n.t("settings.whisper_providers_desc"))
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
        self._liveapi_desc.setText(i18n.t("settings.liveapi_desc"))
        self._liveapi_enabled_label.setText(i18n.t("liveapi.enabled"))
        self._liveapi_soon.setText(i18n.t("liveapi.coming_soon"))
        self._liveapi_prov_label.setText(i18n.t("settings.active_provider"))
        self._liveapi_model_label.setText(i18n.t("settings.model"))
        self._liveapi_docs_btn.setText(i18n.t("provider.browse"))
        self._liveapi_notice.setText(i18n.t("liveapi.notice"))

        self._hier_title.setText(i18n.t("settings.ai_hierarchy"))
        self._hier_sub.setText(i18n.t("settings.ai_hierarchy_sub"))
        self._hier_desc.setText(i18n.t("settings.ai_hierarchy_desc"))
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
        self._cron_desc.setText(i18n.t("settings.cron_desc"))
        self._cron_add_btn.setText("＋ " + i18n.t("cron.add"))
        self._render_cron()

        # Heartbeat (Bloc 5)
        self._hb_title.setText(i18n.t("settings.heartbeat"))
        self._hb_sub.setText(i18n.t("settings.heartbeat_sub"))
        self._hb_desc.setText(i18n.t("settings.heartbeat_desc"))
        self._hb_enabled_label.setText(i18n.t("heartbeat.enabled"))
        self._hb_interval_label.setText(i18n.t("heartbeat.interval"))
        for _name, _label_key, _hint_key, _ht in self._HB_METRICS:
            self._hb_metric_labels[_name].setText(i18n.t(_label_key))
            self._hb_metric_hints[_name].setText(i18n.t(_hint_key))

        # API key fallback hierarchy — title/sub/hint + rebuild the dropdown so
        # the "Select a provider…" placeholder re-translates too.
        self._apikeys_title.setText(i18n.t("settings.api_keys"))
        self._apikeys_sub.setText(i18n.t("settings.api_keys_sub"))
        self._apikeys_desc.setText(i18n.t("settings.api_keys_desc"))
        self._apikeys_hint.setText(i18n.t("api_keys.no_providers"))
        self._refresh_apikeys_providers()

        self._mobile_title.setText(i18n.t("settings.mobile"))
        self._mobile_sub.setText(i18n.t("settings.mobile_sub"))
        self._mobile_desc.setText(i18n.t("settings.mobile_desc"))
        self._mobile_token_label.setText(i18n.t("settings.mobile_token"))
        self._mobile_show_btn.setText(i18n.t("settings.show"))
        self._mobile_qr_btn.setText(i18n.t("settings.show_qr"))
        self._mobile_regen_btn.setText(i18n.t("settings.regenerate"))
        self._mobile_warn.setText(i18n.t("settings.mobile_token_warning"))

        self._legal_title.setText(i18n.t("settings.legal"))
        self._legal_sub.setText(i18n.t("settings.legal_sub"))
        self._legal_desc.setText(i18n.t("settings.legal_desc"))
        self._legal_status.setText(i18n.t("legal.review_status"))
        self._legal_terms_version.setText(
            i18n.t("legal.terms_version", version=legal.TERMS_VERSION)
        )
        self._legal_privacy_version.setText(
            i18n.t("legal.privacy_version", version=legal.PRIVACY_VERSION)
        )
        self._legal_effective_year.setText(
            i18n.t("legal.effective_year", year=legal.PLANNED_EFFECTIVE_YEAR)
        )
        self._legal_contact.setText(
            i18n.t("legal.contact", email=legal.PUBLIC_CONTACT_EMAIL)
        )
        self._legal_governing_law.setText(i18n.t("legal.governing_law"))
        self._legal_terms_btn.setText(i18n.t("legal.open_terms"))
        self._legal_privacy_btn.setText(i18n.t("legal.open_privacy"))
        self._legal_manifest_btn.setText(i18n.t("legal.open_manifest"))

        self._memory_title.setText(i18n.t("settings.memory"))
        self._memory_sub.setText(i18n.t("settings.memory_sub"))
        self._memory_desc.setText(i18n.t("settings.memory_desc"))
        self._memory_refresh_btn.setText(i18n.t("settings.memory_refresh"))
        self._memory_clear_btn.setText(i18n.t("settings.memory_clear_all"))
        self._refresh_memory_entries()

        # Startup section (autostart services)
        self._startup_title.setText(i18n.t("settings.startup"))
        self._startup_sub.setText(i18n.t("settings.startup_sub"))
        self._startup_desc.setText(i18n.t("settings.startup_desc"))
        for _label, _label_key in self._startup_labels:
            _label.setText(i18n.t(_label_key))

        # MCP / Connectors — section chrome + add button, then rebuild the cards
        # so every per-card string (descriptors, status pills, Connect/Disconnect,
        # field labels) re-translates with the new language.
        self._mcp_title.setText(i18n.t("settings.mcp"))
        self._mcp_sub.setText(i18n.t("settings.mcp_sub"))
        self._mcp_desc.setText(i18n.t("settings.mcp_desc"))
        self._mcp_select_label.setText(i18n.t("mcp.select_label"))
        self._mcp_add_btn.setText("＋ " + i18n.t("mcp.add_custom"))
        self._render_mcp()

    def _retranslate_nav(self) -> None:
        """Re-render the master-detail category-nav labels after a language
        change. The QStackedWidget pages stay put; only the QListWidget row text
        is refreshed from the i18n keys captured in ``self._settings_pages``.
        """
        for _row, (_key, _sections) in enumerate(self._settings_pages):
            item = self._settings_nav.item(_row)
            if item is not None:
                item.setText(i18n.t(_key))

    def _on_startup_toggle_changed(self, unit: str, toggle: OsiaSwitch) -> None:
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
        self._refresh_startup_status(force=True)

    def _refresh_startup_status(self, force: bool = False) -> None:
        """Probe each startup service OFF the UI thread.

        ``get_service_status`` / ``is_service_enabled`` shell out to ``tasklist``
        and open a socket to the mobile-server port; on Windows those can take
        ~1s each and were called twice per row, freezing the settings panel for
        ~2s on every open and again every 5s via the refresh timer. Run them on a
        daemon thread and apply the result on the GUI thread via a queued signal
        (WOR perf fix). A guard prevents overlapping probes from stacking up.
        """
        if self._startup_probe_busy:
            if force:
                self._startup_refresh_pending = True
            return
        if (
            not force
            and datetime.now() - self._startup_last_probe < self._STARTUP_STATUS_TTL
        ):
            return
        units = [unit for unit, _dot, _toggle in self._startup_rows]
        if not units:
            return
        self._startup_probe_busy = True

        def _probe():
            results = []
            for unit in units:
                try:
                    state = get_service_status(unit)
                    enabled = is_service_enabled(unit)
                except Exception:
                    state, enabled = "error", False
                results.append((unit, state, enabled))
            # Queued (cross-thread) signal — applied on the GUI thread.
            self._startup_status_ready.emit(results)

        threading.Thread(target=_probe, daemon=True).start()

    def _apply_startup_status(self, results: list) -> None:
        """Apply background startup-status probe results on the GUI thread."""
        self._startup_probe_busy = False
        self._startup_last_probe = datetime.now()
        status = {unit: (state, enabled) for (unit, state, enabled) in results}
        self._startup_refresh_guard = True
        for unit, dot, toggle in self._startup_rows:
            if unit not in status:
                continue
            state, enabled = status[unit]
            if dot.property("state") != state:
                dot.setProperty("state", state)
                dot.style().unpolish(dot)
                dot.style().polish(dot)
            if toggle.isChecked() != enabled:
                toggle.setChecked(enabled)
        self._startup_refresh_guard = False
        if self._startup_refresh_pending:
            self._startup_refresh_pending = False
            QTimer.singleShot(
                0, lambda: self._refresh_startup_status(force=True)
            )

    # ── Visibility hooks ──────────────────────────────────────────────────

    def _apply_local_models_snapshot(self) -> None:
        """Repaint local-model consumers after the worker publishes its cache."""
        if hasattr(self, "_hier_values"):
            self._render_hier_category("heavy")
            self._render_hier_category("medium")
        if hasattr(self, "_cron_host"):
            self._render_cron()
        self._apply_vision_row(load_settings())

    def _schedule_state_refresh_if_stale(self) -> None:
        if (
            datetime.now() - self._state_loaded_at < self._SETTINGS_STATE_TTL
            or self._state_refresh_scheduled
        ):
            return
        self._state_refresh_scheduled = True
        # Let Qt paint the already-cached page before rebuilding stale controls.
        QTimer.singleShot(50, self._refresh_scheduled_state)

    def _refresh_scheduled_state(self) -> None:
        self._state_refresh_scheduled = False
        if not self.isVisible():
            return
        with perf_timer("settings.deferred: _load_state"):
            self._load_state()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_permissions_summary()
        self._schedule_state_refresh_if_stale()
        # Lazy, asynchronous account discovery: constructing OSIA never starts
        # Codex, and opening Settings remains responsive when it is unavailable.
        # Queue the thread starts behind the show event.  The probes themselves
        # remain off-thread; a zero-delay callback also prevents an unrelated
        # delayed probe from racing a Retry/login action shortly afterwards.
        QTimer.singleShot(0, self._refresh_visible_probes)
        self._startup_timer.start()
        self._mcp_timer.start()

    def _refresh_visible_probes(self) -> None:
        if not self.isVisible():
            return
        self.local_models_refresh_requested.emit()
        self._probe_chatgpt_account()
        with perf_timer("settings.deferred: _refresh_startup_status (async)"):
            self._refresh_startup_status()
        self._refresh_mcp_status()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._startup_timer.stop()
        self._mcp_timer.stop()

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
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        host = QWidget()
        host.setObjectName("panelHost")
        v = QVBoxLayout(host)
        # Keep the final authorization/control row fully reachable at the
        # compact 440x560 popup height after long wrapped plan/scope details.
        v.setContentsMargins(0, 0, 6, 56)
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
        enable = OsiaSwitch()
        enable.setChecked(personality.is_skill_enabled(name))
        enable.setToolTip(i18n.t("osia.skill_toggle"))
        enable.stateChanged.connect(
            lambda _s, n=name, c=enable:
                self._on_skill_toggle(n, c)
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
            lambda _=False, n=name, e=editor, b=save_btn, c=enable:
                self._on_skill_save(n, e, b, c)
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

    @staticmethod
    def _on_skill_toggle(name: str, checkbox: OsiaSwitch) -> None:
        effective = personality.set_skill_enabled(name, checkbox.isChecked())
        if checkbox.isChecked() != effective:
            checkbox.blockSignals(True)
            checkbox.setChecked(effective)
            checkbox.blockSignals(False)

    def _toggle_skill_editor(self, wrap: QWidget, editor: QTextEdit,
                             name: str) -> None:
        if wrap.isVisible():
            wrap.setVisible(False)
        else:
            # Reload from disk so the editor reflects any external change.
            editor.setPlainText(personality.read_skill(name))
            wrap.setVisible(True)

    def _on_skill_save(self, name: str, editor: QTextEdit,
                       btn: QPushButton, checkbox: OsiaSwitch) -> None:
        personality.write_skill(name, editor.toPlainText())
        checkbox.blockSignals(True)
        checkbox.setChecked(personality.is_skill_enabled(name))
        checkbox.blockSignals(False)
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


class _AutonomousPanel(QWidget):
    """Autonomous-agent page (WOR-45 Phase 4) — launch and monitor a long,
    multi-step objective that OSIA plans and executes on its own.

    Two stacked sections, styled exactly like the OSIA page (same object names —
    ``section`` / ``sectionTitle`` / ``sectionSub`` / ``mdEditor`` / ``saveBtn``
    / ``ghostBtn`` — so the violet QSS cascades for free, no new stylesheet):

    * **Input** — a Markdown-ish objective editor, a model picker, and a *Start*
      button. While the objective is being planned / executed the button is
      disabled and shows the live state ("Planning…" / "Running…").
    * **Progress** — the live objective, the current phase, an "X / Y tasks"
      counter and a one-line status, plus an *Interrupt* button (pause — the
      task stays resumable on the next launch) and a quieter *Abandon task*
      action (discard).

    The panel is pure UI: it never imports or touches the orchestrator / planner
    / state modules. It only emits intents (:attr:`start_requested`,
    :attr:`interrupt_requested`, :attr:`abort_requested`) and renders the
    snapshots the host feeds back via :meth:`apply_progress` /
    :meth:`seed_from_state`. All engine work happens off the GUI thread in
    ``main.py``.
    """

    # objective, provider, model
    start_requested = pyqtSignal(str, str, str)
    selection_changed = pyqtSignal(str, str)
    authorize_requested = pyqtSignal()
    scope_extension_response = pyqtSignal(str)
    revoke_requested = pyqtSignal()
    interrupt_requested = pyqtSignal()
    abort_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._running = False
        self._planning = False
        self._inventory: list[dict] = []
        self._fallback_reason = ""
        self._run_provider = ""
        self._run_model = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        self._title = QLabel(i18n.t("autonomous.title"))
        self._title.setObjectName("panelTitle")
        header.addWidget(self._title)
        header.addStretch()
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        host = QWidget()
        host.setObjectName("panelHost")
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 6, 0)
        v.setSpacing(14)
        v.addWidget(self._build_input_section())
        v.addWidget(self._build_progress_section())
        v.addStretch(1)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        # Re-translate this panel's chrome when the UI language changes.
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

        self._set_running(False)
        self._reset_progress()

    # ── Input section ───────────────────────────────────────────────────────

    def _build_input_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._input_title = QLabel(i18n.t("autonomous.objective_title"))
        self._input_title.setObjectName("sectionTitle")
        self._input_sub = QLabel(i18n.t("autonomous.objective_sub"))
        self._input_sub.setObjectName("sectionSub")
        self._input_sub.setWordWrap(True)
        l.addWidget(self._input_title)
        l.addWidget(self._input_sub)

        self._objective_edit = QTextEdit()
        self._objective_edit.setObjectName("mdEditor")
        self._objective_edit.setAcceptRichText(False)
        self._objective_edit.setMinimumHeight(110)
        self._objective_edit.setPlaceholderText(
            i18n.t("autonomous.objective_placeholder")
        )
        l.addWidget(self._objective_edit)

        # Model picker — canonical provider/model labels with the exact pair in
        # item data, so duplicate model ids remain distinct.
        self._model_label = QLabel(i18n.t("autonomous.model"))
        self._model_label.setObjectName("sectionSub")
        l.addWidget(self._model_label)
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("voiceCombo")
        _cap_combo_width(self._model_combo)
        self._populate_models()
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        l.addWidget(self._model_combo)

        self._model_notice = QLabel("")
        self._model_notice.setObjectName("sectionSub")
        self._model_notice.setWordWrap(True)
        self._model_notice.hide()
        l.addWidget(self._model_notice)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._start_btn = QPushButton(i18n.t("autonomous.start"))
        self._start_btn.setObjectName("saveBtn")
        self._start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)
        btn_row.addStretch()
        l.addLayout(btn_row)
        return wrap

    def set_inventory(self, inventory: list[dict], resolution: dict) -> None:
        self._inventory = [
            dict(entry) for entry in inventory if isinstance(entry, dict)
        ]
        self._fallback_reason = str(resolution.get("reason") or "")
        self._populate_models(resolution)

    def _populate_models(self, resolution: dict | None = None) -> None:
        """Render the MainPopup-owned cached canonical inventory only."""
        selected = resolution or {}
        wanted = (
            str(selected.get("provider_id") or ""),
            str(selected.get("model_id") or ""),
        )
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        selected_index = -1
        for entry in self._inventory:
            provider_id = str(entry.get("provider_id") or "")
            model_id = str(entry.get("model_id") or "")
            if not provider_id or not model_id:
                continue
            provider_label = str(
                entry.get("provider_display_name") or provider_id
            )
            model_label = str(entry.get("model_display_name") or model_id)
            index = self._model_combo.count()
            self._model_combo.addItem(
                f"{provider_label} — {model_label}",
                (provider_id, model_id),
            )
            if (provider_id, model_id) == wanted:
                selected_index = index
        if selected_index >= 0:
            self._model_combo.setCurrentIndex(selected_index)
        self._model_combo.blockSignals(False)
        self._update_model_notice(bool(selected.get("fallback_occurred")))
        if hasattr(self, "_start_btn"):
            self._start_btn.setEnabled(
                not self._running and self._model_combo.count() > 0
            )
        self._model_combo.setEnabled(
            not self._running and self._model_combo.count() > 0
        )

    def _update_model_notice(self, fallback_occurred: bool | None = None) -> None:
        if not hasattr(self, "_model_notice"):
            return
        if self._model_combo.count() == 0:
            self._model_notice.setText(i18n.t("autonomous.no_models"))
            self._model_notice.show()
        elif fallback_occurred or self._fallback_reason in {
            "saved_selection_unavailable", "legacy_model_ambiguous",
            "legacy_model_unavailable", "saved_selection_incomplete",
            "main_selection_unavailable",
        }:
            self._model_notice.setText(i18n.t("autonomous.model_fallback"))
            self._model_notice.show()
        else:
            self._model_notice.clear()
            self._model_notice.hide()

    def _on_model_changed(self, _index: int) -> None:
        identity = self._model_combo.currentData()
        if (
            isinstance(identity, (tuple, list))
            and len(identity) == 2
            and identity[0]
            and identity[1]
        ):
            self._fallback_reason = ""
            self._update_model_notice(False)
            self.selection_changed.emit(str(identity[0]), str(identity[1]))

    # ── Progress section ────────────────────────────────────────────────────

    def _build_progress_section(self) -> QFrame:
        wrap = QFrame()
        wrap.setObjectName("section")
        l = QVBoxLayout(wrap)
        l.setContentsMargins(14, 12, 14, 12)
        l.setSpacing(8)

        self._progress_title = QLabel(i18n.t("autonomous.progress_title"))
        self._progress_title.setObjectName("sectionTitle")
        l.addWidget(self._progress_title)

        self._prog_objective = QLabel("—")
        self._prog_objective.setObjectName("sectionSub")
        self._prog_objective.setWordWrap(True)
        l.addWidget(self._prog_objective)

        self._prog_model = QLabel("")
        self._prog_model.setObjectName("sectionSub")
        self._prog_model.hide()
        l.addWidget(self._prog_model)

        self._prog_phase = QLabel("")
        self._prog_phase.setObjectName("sectionSub")
        self._prog_phase.setWordWrap(True)
        l.addWidget(self._prog_phase)

        self._prog_counter = QLabel("")
        self._prog_counter.setObjectName("sectionSub")
        l.addWidget(self._prog_counter)

        self._prog_status = QLabel("")
        self._prog_status.setObjectName("sectionSub")
        self._prog_status.setWordWrap(True)
        l.addWidget(self._prog_status)

        self._plan_review = QLabel("")
        self._plan_review.setObjectName("sectionSub")
        self._plan_review.setWordWrap(True)
        self._plan_review.hide()
        l.addWidget(self._plan_review)

        self._capability_review = QLabel("")
        self._capability_review.setObjectName("sectionSub")
        self._capability_review.setWordWrap(True)
        self._capability_review.hide()
        l.addWidget(self._capability_review)

        self._authorization_state = QLabel("")
        self._authorization_state.setObjectName("sectionSub")
        self._authorization_state.setWordWrap(True)
        self._authorization_state.hide()
        l.addWidget(self._authorization_state)

        self._scope_frame = QFrame()
        self._scope_frame.setObjectName("section")
        scope_layout = QVBoxLayout(self._scope_frame)
        scope_layout.setContentsMargins(10, 10, 10, 10)
        scope_layout.setSpacing(8)
        self._scope_title = QLabel(i18n.t("autonomous.scope_extension_title"))
        self._scope_title.setObjectName("sectionTitle")
        self._scope_detail = QLabel("")
        self._scope_detail.setObjectName("sectionSub")
        self._scope_detail.setWordWrap(True)
        scope_layout.addWidget(self._scope_title)
        scope_layout.addWidget(self._scope_detail)
        scope_buttons = QHBoxLayout()
        self._scope_approve_btn = QPushButton(
            i18n.t("autonomous.approve_exact")
        )
        self._scope_approve_btn.setObjectName("saveBtn")
        self._scope_approve_btn.clicked.connect(
            lambda: self.scope_extension_response.emit("approve_exact")
        )
        self._scope_deny_btn = QPushButton(i18n.t("autonomous.deny"))
        self._scope_deny_btn.setObjectName("ghostBtn")
        self._scope_deny_btn.clicked.connect(
            lambda: self.scope_extension_response.emit("deny")
        )
        scope_buttons.addWidget(self._scope_approve_btn)
        scope_buttons.addWidget(self._scope_deny_btn)
        scope_layout.addLayout(scope_buttons)
        self._scope_frame.hide()
        l.addWidget(self._scope_frame)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._interrupt_btn = QPushButton(i18n.t("autonomous.interrupt"))
        self._interrupt_btn.setObjectName("ghostBtn")
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.clicked.connect(self._on_interrupt)
        # "Abandon" is the quieter, secondary action — same ghost pill, placed
        # after Interrupt so the destructive choice never reads as primary.
        self._abort_btn = QPushButton(i18n.t("autonomous.abort"))
        self._abort_btn.setObjectName("ghostBtn")
        self._abort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._abort_btn.clicked.connect(self._on_abort)
        self._authorize_btn = QPushButton(
            i18n.t("autonomous.authorize_and_start")
        )
        self._authorize_btn.setObjectName("saveBtn")
        self._authorize_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._authorize_btn.clicked.connect(self.authorize_requested.emit)
        self._authorize_btn.hide()
        self._revoke_btn = QPushButton(i18n.t("autonomous.revoke"))
        self._revoke_btn.setObjectName("ghostBtn")
        self._revoke_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._revoke_btn.clicked.connect(self.revoke_requested.emit)
        self._revoke_btn.hide()
        btn_row.addWidget(self._authorize_btn)
        btn_row.addWidget(self._revoke_btn)
        btn_row.addWidget(self._interrupt_btn)
        btn_row.addWidget(self._abort_btn)
        btn_row.addStretch()
        l.addLayout(btn_row)
        return wrap

    # ── User intents ────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        if self._running:
            return
        objective = self._objective_edit.toPlainText().strip()
        if not objective:
            return
        identity = self._model_combo.currentData()
        if not isinstance(identity, (tuple, list)) or len(identity) != 2:
            return
        provider = str(identity[0] or "").strip().lower()
        model = str(identity[1] or "").strip()
        if not provider or not model:
            return
        self._prog_objective.setText(objective)
        # Lock the panel immediately (planning runs off the GUI thread); the host
        # flips us to "running" or re-opens us via a progress event.
        self._set_running(True, planning=True)
        self._set_status(i18n.t("autonomous.status_planning"))
        self.start_requested.emit(objective, provider, model)

    def _on_interrupt(self) -> None:
        self.interrupt_requested.emit()

    def _on_abort(self) -> None:
        self.abort_requested.emit()

    # ── Host-driven display updates ─────────────────────────────────────────

    def apply_progress(self, payload: dict) -> None:
        """Render a progress event the host marshalled onto the GUI thread.

        ``payload`` is an orchestrator event dict (``event`` + optional
        ``reason``) merged with a state snapshot (``objective`` / ``phase_name``
        / ``done`` / ``total``). Terminal events release the panel back to the
        input state; running / informational events keep it locked.
        """
        if not isinstance(payload, dict):
            return
        event = payload.get("event", "")
        self._render_snapshot(payload)
        diagnostic_text = self._diagnostic_text(payload)

        if event in {"autonomous_plan_ready", "authorization_pending"}:
            self._set_running(True)
            self._interrupt_btn.setEnabled(False)
            self._abort_btn.setEnabled(True)
            self._authorize_btn.show()
            self._authorize_btn.setEnabled(True)
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(i18n.t("autonomous.status_authorization_pending"))
        elif event == "running":
            self._set_running(True)
            self._authorize_btn.hide()
            self._revoke_btn.show()
            self._scope_frame.hide()
            self._set_status(i18n.t("autonomous.status_running"))
        elif event in {
            "action_starting", "action_in_progress", "waiting_tool",
            "verifying_tool", "resuming", "cancellation_requested",
        }:
            self._set_running(True)
            self._authorize_btn.hide()
            self._revoke_btn.show()
            self._scope_frame.hide()
            if event == "cancellation_requested":
                self._interrupt_btn.setEnabled(False)
                self._abort_btn.setEnabled(False)
            self._set_status(i18n.t({
                "action_starting": "autonomous.status_action_starting",
                "action_in_progress": "autonomous.status_action_in_progress",
                "waiting_tool": "autonomous.status_waiting_tool",
                "verifying_tool": "autonomous.status_verifying_tool",
                "resuming": "autonomous.status_resuming",
                "cancellation_requested": (
                    "autonomous.status_cancellation_requested"
                ),
            }[event]))
        elif event == "blocked_unknown_effect":
            self._set_running(True)
            self._interrupt_btn.setEnabled(False)
            self._abort_btn.setEnabled(True)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(
                i18n.t("autonomous.status_blocked_unknown_effect")
            )
        elif event == "cancelled_proven":
            self._set_running(False)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(i18n.t("autonomous.status_cancelled_proven"))
        elif event in {
            "autonomous_scope_extension_request", "suspended_scope_extension",
            "scope_extension_denied",
        }:
            self._set_running(True)
            self._interrupt_btn.setEnabled(False)
            self._abort_btn.setEnabled(True)
            self._authorize_btn.hide()
            self._revoke_btn.show()
            self._scope_frame.show()
            pending = payload.get("pending_scope_extension") or {}
            barrier = str(pending.get("universal_barrier") or "")
            responded = pending.get("responded") is True
            self._scope_approve_btn.setEnabled(
                pending.get("can_approve_pc", True) is True
                and not bool(barrier)
                and not responded
            )
            self._scope_deny_btn.setEnabled(not responded)
            self._set_status(i18n.t("autonomous.status_scope_extension"))
        elif event in {
            "autonomous_authorization_revoked", "suspended_authorization_revoked",
        }:
            self._set_running(True)
            self._interrupt_btn.setEnabled(False)
            self._abort_btn.setEnabled(True)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(i18n.t("autonomous.status_revoked"))
        elif event in {"autonomous_outcome_unknown", "blocked_outcome_unknown"}:
            self._set_running(True)
            self._interrupt_btn.setEnabled(False)
            self._abort_btn.setEnabled(True)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(i18n.t("autonomous.status_outcome_unknown"))
        elif event == "authorization_error":
            self._set_status(i18n.t("autonomous.status_authorization_error"))
        elif event == "task_done":
            self._set_status(i18n.t("autonomous.status_task_done"))
        elif event == "salvaged":
            self._set_status(i18n.t("autonomous.status_salvaged"))
        elif event == "task_failed":
            self._set_status(diagnostic_text)
        elif event == "done":
            self._set_running(False)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            final_answer = payload.get("final_answer")
            self._set_status(
                final_answer[:8_000]
                if isinstance(final_answer, str) and final_answer.strip()
                else i18n.t("autonomous.status_done")
            )
        elif event == "failed":
            self._set_running(False)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(diagnostic_text)
        elif event == "interrupted":
            self._set_running(False)
            self._set_status(i18n.t("autonomous.status_interrupted"))
        elif event == "aborted":
            self._set_running(False)
            self._authorize_btn.hide()
            self._revoke_btn.hide()
            self._scope_frame.hide()
            self._set_status(i18n.t("autonomous.status_aborted"))
        elif event == "plan_failed":
            self._set_running(False)
            self._set_status(diagnostic_text)

    def seed_from_state(self, state: dict) -> None:
        """Seed the display from a persisted ``task_state`` dict — used when the
        host resumes an interrupted task or opens the panel on one."""
        if not isinstance(state, dict):
            return
        objective = state.get("objective") or ""
        if objective:
            self._objective_edit.setPlainText(objective)
        snapshot = self._snapshot_from_state(state)
        self._render_snapshot(snapshot)
        run_state = state.get("run_state") or ""
        event = {
            "authorization_pending": "autonomous_plan_ready",
            "ready": "authorization_pending",
            "running": "running",
            "waiting_tool": "waiting_tool",
            "verifying_tool": "verifying_tool",
            "resuming": "resuming",
            "blocked_unknown_effect": "blocked_unknown_effect",
            "cancellation_requested": "cancellation_requested",
            "cancelled_proven": "cancelled_proven",
            "suspended_scope_extension": "suspended_scope_extension",
            "suspended_authorization_revoked": "suspended_authorization_revoked",
            "blocked_outcome_unknown": "blocked_outcome_unknown",
            "completed": "done",
            "failed": "failed",
            "cancelled": "aborted",
        }.get(run_state)
        if event:
            self.apply_progress({"event": event, **snapshot})
            return
        status = state.get("status") or ""
        if status == "in_progress":
            self._set_running(True)
            self._set_status(i18n.t("autonomous.status_running"))
        elif status == "failed":
            self._set_running(False)
            self._set_status(self._diagnostic_text(snapshot))
        elif status == "aborted":
            self._set_running(False)
            self._set_status(i18n.t("autonomous.status_aborted"))
        else:
            self._set_running(False)
            self._set_status(i18n.t("autonomous.status_idle"))

    # ── Internal display helpers ────────────────────────────────────────────

    def _render_snapshot(self, payload: dict) -> None:
        """Update the objective / phase / counter labels from a snapshot dict,
        leaving any field the payload omits untouched."""
        objective = payload.get("objective")
        if objective:
            self._prog_objective.setText(objective)
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        if provider and model:
            self._run_provider = provider
            self._run_model = model
            self._render_run_model()
        phase_name = payload.get("phase_name")
        if (payload.get("error_category")
                or payload.get("event") in {"task_failed", "failed", "plan_failed"}):
            self._prog_phase.setText("")
        elif phase_name:
            self._prog_phase.setText(i18n.t("autonomous.phase", name=phase_name))
        total = payload.get("total")
        if total is not None:
            done = payload.get("done", 0) or 0
            self._prog_counter.setText(
                i18n.t("autonomous.tasks", done=done, total=total)
            )
        plan_summary = payload.get("plan_summary")
        if isinstance(plan_summary, list):
            plan_lines = []
            for phase in plan_summary:
                if not isinstance(phase, dict):
                    continue
                name = self._short(str(phase.get("name") or ""), 100)
                if name:
                    plan_lines.append(name)
                for task in phase.get("tasks") or []:
                    plan_lines.append(f"  • {self._short(str(task), 140)}")
            self._plan_review.setText(
                i18n.t("autonomous.plan_review") + "\n" + "\n".join(plan_lines)
            )
            self._plan_review.setVisible(bool(plan_lines))
        capabilities = payload.get("capability_summary")
        if isinstance(capabilities, list):
            private = payload.get("private_scope_details") or {}
            capability_lines = []
            for item in capabilities:
                if not isinstance(item, dict):
                    continue
                label = self._short(str(item.get("label") or ""), 100)
                effect = self._short(str(item.get("effect_class") or ""), 60)
                precision = self._short(
                    str(item.get("target_precision") or "unknown"), 40,
                )
                detail = self._short(
                    str(private.get(item.get("capability_id")) or ""), 160,
                )
                suffix = f" — {detail}" if detail else ""
                bounds = []
                uses = item.get("expected_use_count")
                ttl = item.get("grant_ttl_seconds")
                if type(uses) is int and uses > 0:
                    bounds.append(f"{uses}×")
                if type(ttl) is int and ttl > 0:
                    bounds.append(f"{max(1, ttl // 60)} min")
                bounds_text = f" · {' · '.join(bounds)}" if bounds else ""
                capability_lines.append(
                    f"• {label} · {effect} · {precision}{bounds_text}{suffix}"
                )
            self._capability_review.setText(
                i18n.t("autonomous.capability_review")
                + "\n" + "\n".join(capability_lines)
            )
            self._capability_review.setVisible(bool(capability_lines))
        source = payload.get("authorization_source")
        if source:
            grant_count = len(payload.get("grant_refs") or [])
            self._authorization_state.setText(i18n.t(
                "autonomous.authorization_state",
                source=str(source),
                count=grant_count,
            ))
            self._authorization_state.show()
        pending = payload.get("pending_scope_extension")
        if isinstance(pending, dict):
            local_note = (
                i18n.t("autonomous.local_pc_review")
                if pending.get("local_pc_review_required") else ""
            )
            detail_text = i18n.t(
                "autonomous.scope_extension_detail",
                category=str(pending.get("safe_category") or ""),
                effect=str(pending.get("effect_class") or ""),
                uses=int(pending.get("requested_use_count", 1) or 1),
                local=local_note,
            )
            private_detail = self._short(
                str(pending.get("private_scope_detail") or ""), 320,
            )
            try:
                ttl_minutes = max(1, int(
                    (float(pending.get("expires_at"))
                     - float(pending.get("issued_at"))) / 60
                ))
            except (TypeError, ValueError, OverflowError):
                ttl_minutes = 0
            if ttl_minutes:
                detail_text += f" · {ttl_minutes} min"
            if private_detail:
                detail_text += "\n" + private_detail
            self._scope_detail.setText(detail_text)

    @staticmethod
    def _snapshot_from_state(state: dict) -> dict:
        """Compute objective / phase_name / done / total from a task_state dict
        (mirrors the host's snapshot so seed and live events render alike)."""
        phases = state.get("phases") or []
        total = 0
        done = 0
        for ph in phases:
            for tk in (ph.get("tasks") or []):
                total += 1
                if tk.get("status") == "done":
                    done += 1
        cursor = state.get("cursor") or {}
        p = int(cursor.get("phase", 0) or 0)
        phase_name = ""
        if 0 <= p < len(phases):
            phase_name = phases[p].get("name", "") or ""
        return {
            "objective": state.get("objective", "") or "",
            "provider": str(state.get("provider") or ""),
            "model": str(state.get("model") or ""),
            "phase_name": phase_name,
            "done": done,
            "total": total,
            "run_state": state.get("run_state", ""),
            "authorization_source": state.get("authorization_source", "none"),
            "grant_refs": list(state.get("grant_refs") or []),
            "capability_summary": list(state.get("capability_summary") or []),
            "pending_scope_extension": state.get("pending_scope_extension"),
            "private_scope_details": dict(
                state.get("_runtime_pc_capability_details") or {}
            ),
            "plan_summary": [
                {
                    "name": str(phase.get("name") or ""),
                    "tasks": [
                        str(task.get("objective") or "")
                        for task in phase.get("tasks") or []
                        if isinstance(task, dict)
                    ],
                }
                for phase in phases if isinstance(phase, dict)
            ],
            **autonomous_diagnostics.projection(state),
        }

    @staticmethod
    def _diagnostic_text(payload: dict) -> str:
        """Localized, category-based text; never renders backend free-form text."""
        category = payload.get("error_category") or "unknown_failure"
        parts = [i18n.t(autonomous_diagnostics.failure_message_key(category))]
        attempt = payload.get("attempt")
        max_attempts = payload.get("max_attempts")
        if isinstance(attempt, int) and attempt > 0 and isinstance(max_attempts, int):
            parts.append(i18n.t(
                "autonomous.diagnostic_attempt",
                attempt=attempt,
                max_attempts=max_attempts,
            ))
        replan_count = payload.get("replan_count")
        max_replans = payload.get("max_replans")
        if (payload.get("stage") == "replan" and isinstance(replan_count, int)
                and isinstance(max_replans, int)):
            parts.append(i18n.t(
                "autonomous.diagnostic_replans",
                replan_count=replan_count,
                max_replans=max_replans,
            ))
        return " ".join(part for part in parts if part)

    def _set_running(self, running: bool, *, planning: bool = False) -> None:
        """Toggle the input/progress controls between idle and busy states.

        Interrupt / Abandon are enabled ONLY once the orchestrator is actually
        running — NOT during the planning window. Planning happens off-thread
        before ``orchestrator.start()``, and stop()/start() would race if a
        cancel landed mid-plan (the start would clear the stop signal and run the
        task the user just abandoned). Keeping them disabled until the host emits
        the "running" event closes that window."""
        self._running = running
        self._planning = bool(running and planning)
        self._start_btn.setEnabled(
            not running and self._model_combo.count() > 0
        )
        if running:
            self._start_btn.setText(
                i18n.t("autonomous.planning") if planning
                else i18n.t("autonomous.running")
            )
        else:
            self._start_btn.setText(i18n.t("autonomous.start"))
        self._objective_edit.setReadOnly(running)
        self._model_combo.setEnabled(
            not running and self._model_combo.count() > 0
        )
        controllable = running and not planning
        self._interrupt_btn.setEnabled(controllable)
        self._abort_btn.setEnabled(controllable)

    def _reset_progress(self) -> None:
        self._prog_objective.setText("—")
        self._run_provider = ""
        self._run_model = ""
        self._render_run_model()
        self._prog_phase.setText("")
        self._prog_counter.setText("")
        self._set_status(i18n.t("autonomous.status_idle"))

    def _set_status(self, text: str) -> None:
        self._prog_status.setText(text)

    def _render_run_model(self) -> None:
        if self._run_provider and self._run_model:
            self._prog_model.setText(
                f"{i18n.t('autonomous.model')}: "
                f"{self._run_provider} — {self._run_model}"
            )
            self._prog_model.show()
        else:
            self._prog_model.clear()
            self._prog_model.hide()

    @staticmethod
    def _short(text, limit: int = 120) -> str:
        return " ".join((text or "").split())[:limit]

    def _retranslate(self) -> None:
        self._title.setText(i18n.t("autonomous.title"))
        self._input_title.setText(i18n.t("autonomous.objective_title"))
        self._input_sub.setText(i18n.t("autonomous.objective_sub"))
        self._objective_edit.setPlaceholderText(
            i18n.t("autonomous.objective_placeholder")
        )
        self._model_label.setText(i18n.t("autonomous.model"))
        self._render_run_model()
        self._progress_title.setText(i18n.t("autonomous.progress_title"))
        self._scope_title.setText(i18n.t("autonomous.scope_extension_title"))
        self._authorize_btn.setText(i18n.t("autonomous.authorize_and_start"))
        self._revoke_btn.setText(i18n.t("autonomous.revoke"))
        self._scope_approve_btn.setText(i18n.t("autonomous.approve_exact"))
        self._scope_deny_btn.setText(i18n.t("autonomous.deny"))
        self._interrupt_btn.setText(i18n.t("autonomous.interrupt"))
        self._abort_btn.setText(i18n.t("autonomous.abort"))
        if not self._running:
            self._start_btn.setText(i18n.t("autonomous.start"))
        self._update_model_notice()
        # The live status/phase/counter text reflects the last snapshot and
        # re-renders (translated) on the next progress event.


# ── Bottom-nav button ────────────────────────────────────────────────────────

class _NavButton(QPushButton):
    """A bottom-nav button that paints a crisp vector icon.

    Qt's emoji rendering is inconsistent across platforms, so each nav entry
    draws its own glyph via ``QPainterPath``:

    * ``conv``       — a rounded speech bubble (Conversations)
    * ``models``     — a four-point sparkle/star (Models)
    * ``settings``   — a gear (Settings)
    * ``osia``       — a person/identity silhouette (OSIA personality + skills)
    * ``autonomous`` — an infinity symbol / lemniscate (Autonomous agent)

    The active tab gets a rounded violet pill behind the icon and the icon
    itself switches to the violet accent; hover shows a softer violet wash.
    """

    ICON_PX = 29    # WOR-48 P3: 33 → 29, slightly smaller nav glyphs

    def __init__(self, kind: str, tooltip: str):
        super().__init__()
        self._kind = kind
        # i18n key for the tooltip, set by the owner; None for proper nouns
        # (e.g. "OSIA") that are shown verbatim and never translated.
        self._tip_key: str | None = None
        self.setObjectName("navBtn")
        self.setCheckable(True)
        # The button must stay taller than the painted glyphs — notably the
        # speech-bubble tail, which reaches ~0.22*ICON_PX below the bubble
        # body — so they never clip against its bottom edge. WOR-48 P3:
        # 52×56 → 46×50, slightly smaller buttons to match the smaller icons
        # (ICON_PX 29: the tail bottoms out ~17px below centre, well inside
        # the 25px half-height).
        self.setFixedSize(46, 50)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._hover_progress = 0.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(MOTION.micro_ms)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_progress)

    def enterEvent(self, event):
        self._hovered = True
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def _animate_hover(self, target: float) -> None:
        self._hover_anim.stop()
        if reduced_motion_requested(self):
            self._hover_progress = target
            self.update()
            return
        self._hover_anim.setStartValue(self._hover_progress)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def _on_hover_progress(self, value) -> None:
        self._hover_progress = float(value)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = get_theme_manager().palette

        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        active = self.isChecked()

        # Rounded pill background — a subtle violet wash when active (no border
        # line), a softer one on hover.
        if self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.color("primary", 55))
            p.drawRoundedRect(rect, 9, 9)
        elif active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(palette.color("primary", 31))
            p.drawRoundedRect(rect, 10, 10)
        elif self._hover_progress > 0.0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(
                palette.color("primary", round(36 * self._hover_progress))
            )
            p.drawRoundedRect(rect, 12, 12)

        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(palette.color("focus", 190), 1.2))
            p.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 9, 9)

        # Icon colour: violet accent when active, dim white otherwise.
        color = (palette.color("primary") if active
                 else palette.color("text", 200))

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
        elif self._kind == "autonomous":
            self._paint_infinity(p, cx, cy, s, color)
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

    def _paint_infinity(self, p, cx, cy, s, color) -> None:
        """A lemniscate (∞) — two small loops meeting at a central crossing,
        traced as one continuous stroke. Marks the Autonomous-agent section
        and mirrors the infinity glyph now used by the mobile app."""
        ox = s * 0.42      # half-width: centre → outer edge of each loop
        oy = s * 0.28      # vertical control reach (true height ≈ 1.5·oy)
        ix = s * 0.20      # inner control reach — sets loop fullness & crossing
        path = QPainterPath()
        path.moveTo(cx, cy)
        # Right loop: over the top out to the far-right point, back under to centre.
        path.cubicTo(cx + ix, cy - oy, cx + ox, cy - oy, cx + ox, cy)
        path.cubicTo(cx + ox, cy + oy, cx + ix, cy + oy, cx, cy)
        # Left loop: continues smoothly through the crossing, mirrored on x.
        path.cubicTo(cx - ix, cy - oy, cx - ox, cy - oy, cx - ox, cy)
        path.cubicTo(cx - ox, cy + oy, cx - ix, cy + oy, cx, cy)
        # Stroked like the speech-bubble glyph: same pen width, no fill.
        p.setPen(QPen(color, 2.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


# ── Central page stack ────────────────────────────────────────────────────────

class _CentralStack(QStackedWidget):
    """The QStackedWidget that hosts MainPopup's pages, with ONE behavioural
    tweak that completes the Settings-reopen hit-test fix (see the
    ``SetNoConstraint`` / Ignored-vertical notes in ``MainPopup.__init__``).

    Root cause (proven by headless sizeHint instrumentation): the Settings page
    carries a word-wrapped title ``QLabel#panelTitle`` — the only panel title
    that wraps — so that page, and therefore THIS stack, reports
    ``hasHeightForWidth() == True``. When a top-level ``QVBoxLayout`` sizes a
    child that has height-for-width it sizes it via ``heightForWidth()`` instead
    of ``sizeHint()``, and that path is IMMUNE to the ``QSizePolicy.Ignored``
    vertical zeroing applied to this stack. So with Settings active the stack's
    tall max-page content height (≈337 with these fonts, ≈440 with Windows
    fonts) leaks straight into ``MainPopup.sizeHint()`` (≈620 on Windows). On
    the frameless + translucent Win32 window the native HWND then maps to that
    oversized hint PAST ``setFixedHeight(POPUP_H)`` — the ~60px dead native
    strip that mis-routes clicks/hover on reopen. Other pages (osia/autonomous)
    tie that height via their own ``QScrollArea`` but do NOT trigger the bug
    because their titles do not wrap, so they have no height-for-width and the
    Ignored vertical policy already nulls them.

    Reporting ``hasHeightForWidth() == False`` here forces the top-level layout
    back onto the (zeroable) ``sizeHint`` path, so the Ignored vertical policy
    set in ``__init__`` can null this stack's height contribution for EVERY
    page, Settings included → ``MainPopup.sizeHint()`` collapses to its chrome
    height (well under ``POPUP_H``) and the explicit ``setFixedHeight(POPUP_H)``
    minimum becomes the sole authority, so the native HWND maps to POPUP_H.

    Nothing else changes: the stack is given its height by its stretch factor in
    the outer layout (``addWidget(self._stack, 1)``) and distributes it to the
    current page exactly as before, so every page — and the Settings
    ``QScrollArea`` — is sized and scrolls unchanged. Each page's OWN internal
    layout still honours height-for-width for its word-wrapped labels (this
    override only affects how the TOP-LEVEL layout measures the stack).
    """

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt method name)
        return False


# ── MainPopup ───────────────────────────────────────────────────────────────

class MainPopup(QWidget):
    """Floating glass panel anchored just below the Dynamic Island."""

    send_text = pyqtSignal(str)
    submit_draft = pyqtSignal(str, object)
    submit_directive_draft = pyqtSignal(str, object, object)
    informational_requested = pyqtSignal(str)
    pick_file = pyqtSignal(str, str)
    new_conversation = pyqtSignal()
    open_conversation = pyqtSignal(str)
    rename_conversation = pyqtSignal(str, str)
    delete_conversation = pyqtSignal(str)
    folder_mutation_requested = pyqtSignal(dict)
    bulk_delete_ungrouped_requested = pyqtSignal(int)
    model_changed = pyqtSignal(str, str)
    api_keys_saved = pyqtSignal(dict)
    chatgpt_status_changed = pyqtSignal(dict)
    # Re-emitted from the settings panel: the floating-capsule visibility toggle
    # (WOR-22). The host wires it to the Dynamic Island's set_capsule_visible.
    capsule_visibility_changed = pyqtSignal(bool)
    # Compatibility signal retained for older hosts; the disabled settings row
    # can only emit False in this release.
    wake_word_enabled_changed = pyqtSignal(bool)
    # Re-emitted from the autonomous panel (WOR-45 Phase 4). The host wires these
    # to the dedicated Orchestrator: start (objective, provider, model), pause
    # (resumable next launch) and discard.
    autonomous_start_requested = pyqtSignal(str, str, str)
    autonomous_authorize_requested = pyqtSignal()
    autonomous_scope_extension_response = pyqtSignal(str)
    autonomous_revoke_requested = pyqtSignal()
    autonomous_interrupt_requested = pyqtSignal()
    autonomous_abort_requested = pyqtSignal()
    _ollama_models_ready = pyqtSignal(list)
    _permission_epoch_changed = pyqtSignal(int)

    # Index of the chat page in the QStackedWidget.
    PAGE_CHAT = 0
    PAGE_CONVS = 1
    PAGE_MODELS = 2
    PAGE_SETTINGS = 3
    PAGE_OSIA = 4
    PAGE_AUTONOMOUS = 5

    def __init__(self):
        super().__init__()

        self._update_controller = UpdateController(self)

        self._theme = get_theme_manager()
        self._motion = MotionController(
            self,
            reduced_motion=lambda: reduced_motion_requested(self),
        )
        self._window_opacity_anim = QPropertyAnimation(
            self, b"windowOpacity", self
        )
        self._window_opacity_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._window_opacity_anim.finished.connect(
            self._on_window_opacity_finished
        )
        self._hiding = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # WOR-48 P3 — the width is dynamic per page (COMPACT ↔ WIDE, see
        # _animate_width), so the window is no longer setFixedSize: the height
        # stays locked at POPUP_H while the width is only bounded so the
        # geometry animation can tween it. The window has no native resize
        # edges (frameless Tool window), so only _animate_width changes it.
        self.setFixedHeight(POPUP_H)
        self.setMinimumWidth(POPUP_W)
        self.setMaximumWidth(POPUP_W_WIDE)
        self.resize(POPUP_W, POPUP_H)

        # WOR-48 audit #2 — the 440<->640 width change now SNAPS instantly (see
        # _animate_width). The old geometry tween slid the settings category nav
        # under the cursor mid-flight, so the first category click frequently
        # missed (~50% settings "freeze"). The QPropertyAnimation object is kept
        # only so _finish_width_anim() stays a harmless no-op; it is never started.
        self._width_anim = QPropertyAnimation(self, b"geometry", self)
        self._width_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # The Dynamic Island capsule the popup anchors to (set_anchor) —
        # when set, the popup opens just below the capsule's CURRENT
        # position instead of a fixed screen coordinate.
        self._anchor: QWidget | None = None
        # WOR-74: window dragging is delegated to the OS via startSystemMove()
        # (see mousePressEvent) — there is no Python-side drag state to hold.

        # Soft shadow — WOR-48 audit #2: NO QGraphicsDropShadowEffect. An
        # installed graphics effect makes Qt render the widget into an off-screen
        # buffer, which defeats the per-frame transparent clear in paintEvent and
        # leaves ghost rectangles when this translucent window is dragged/resized.
        # The non-Windows drop shadow is instead painted by hand in paintEvent,
        # inside the SHADOW_MARGIN canvas band → it lives in the backing store
        # and is wiped clean every frame (no ghosts). WOR-237 skips only those
        # rings on Windows. No setGraphicsEffect anywhere.

        outer = QVBoxLayout(self)
        # Settings-reopen hit-test fix (root cause). MainPopup is a top-level
        # window, so this top-level layout defaults to SetDefaultConstraint —
        # whose activate() does `window.setMinimumSize(layout.totalMinimumSize())`
        # on every layout pass. totalMinimumSize() is dominated by the central
        # QStackedWidget's minimumSize (= MAX over ALL pages = the Settings
        # master-detail page, ~800x700). Once Settings has been laid out once its
        # heavy minimumSize is cached, so on the NEXT reopen the layout forces the
        # window minimum to ~800x700 — overriding setFixedHeight(POPUP_H) (min-h
        # 700 > max-h 560 ⇒ the native window grows to ~775) and setMaximumWidth
        # (POPUP_W_WIDE). That oversized native top-level (~800x775 around 640x560
        # of painted content) is what mis-routes mouse events: clicks/hover land
        # in the dead oversized region and never reach the inner widgets. It also
        # re-fires AFTER anchor_below_island()'s setGeometry on the post-show
        # LayoutRequest, so pinning geometry / WA_Resized there cannot hold it.
        # SetNoConstraint makes activate() a no-op for the window's min/max, so
        # the explicit setFixedHeight/min/max-width below are the SOLE size
        # authority and the per-page setGeometry (440 or 640 × 560) always wins.
        # Settings content that exceeds the height scrolls inside its own
        # QScrollArea exactly as before — the window never grows to fit it.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        # WOR-48 audit #2: outer margin = SHADOW_MARGIN (transparent canvas band
        # for the hand-painted drop shadow that replaced QGraphicsDropShadowEffect)
        # + the card's own 11px internal padding, preserved from before. Total
        # ~38px; the card itself is inset by SHADOW_MARGIN in paintEvent.
        _m = SHADOW_MARGIN + 11
        outer.setContentsMargins(_m, _m, _m, _m)
        outer.setSpacing(6)

        # Header — OSIA logo on the left, close button on the right. The logo
        # alone represents OSIA, so no separate "OSIA" wordmark is shown.
        header = QHBoxLayout()
        header.setSpacing(8)

        logo = QLabel()
        logo.setObjectName("popupLogo")
        logo_pix = QPixmap(LOGO_PATH)
        if not logo_pix.isNull():
            # Render at the screen's device-pixel ratio so the logo stays crisp
            # on high-DPI / fractionally-scaled Windows displays: scale to
            # 44·dpr physical px and tag the pixmap with that ratio, so Qt shows
            # it at 44 logical px from native-resolution pixels (no upscale
            # blur). dpr == 1.0 reproduces the previous result exactly.
            dpr = logo.devicePixelRatioF() or 1.0
            scaled = logo_pix.scaledToHeight(
                max(1, round(44 * dpr)),
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            logo.setPixmap(scaled)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("closeBtn")
        # WOR-48 P3: slightly larger, SQUARE with lightly rounded corners
        # (radius in the #closeBtn QSS) instead of the previous 26px circle.
        close_btn.setFixedSize(30, 30)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self._hide_animated)

        self._full_access_indicator = QLabel()
        self._full_access_indicator.setObjectName("fullAccessIndicator")
        self._full_access_indicator.hide()

        header.addWidget(logo)
        header.addStretch()
        header.addWidget(
            self._full_access_indicator, 0, Qt.AlignmentFlag.AlignVCenter,
        )
        header.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header)

        self._update_banner = UpdateBanner(self)
        outer.addWidget(self._update_banner)

        # Central stack
        # Settings-reopen hit-test fix (APPROACH A — the Qt-recommended lever).
        # Two cooperating measures stop the central stack's tall max-page content
        # height from inflating MainPopup.sizeHint() (and thus the native HWND on
        # the frameless+translucent Win32 window) past setFixedHeight(POPUP_H):
        #   1. _CentralStack.hasHeightForWidth() -> False (see that class) keeps
        #      the Settings page's word-wrapped-title height-for-width OFF the
        #      top-level layout's height-for-width path.
        #   2. The Ignored VERTICAL size policy below makes this stack's height
        #      sizeHint a no-op for the top-level layout's totalSizeHint.
        # The stack still FILLS the window via its stretch factor (addWidget
        # below) and clips/scrolls each page's content exactly as before — only
        # the sizeHint propagation to the top level changes. This pairs with the
        # outer-layout SetNoConstraint above (which caps the window MINIMUM); the
        # two together leave setFixedHeight(POPUP_H) / min/max-width as the sole
        # size authority, so the native HWND maps to POPUP_W(_WIDE) x POPUP_H.
        self._stack = _CentralStack()
        _stack_sp = self._stack.sizePolicy()
        _stack_sp.setVerticalPolicy(QSizePolicy.Policy.Ignored)
        self._stack.setSizePolicy(_stack_sp)
        self._chat = _ChatPage()
        self._chat.send_text.connect(self.send_text.emit)
        self._chat.submit_draft.connect(self.submit_draft.emit)
        self._chat.submit_directive_draft.connect(
            self.submit_directive_draft.emit
        )
        self._chat.informational_requested.connect(
            self.informational_requested.emit
        )
        self._chat.pick_file.connect(self.pick_file.emit)

        self._convs = _ConversationsPanel()
        self._convs.new_conversation.connect(self._on_new_conv)
        self._convs.open_conversation.connect(self._on_open_conv)
        self._convs.rename_conversation.connect(self.rename_conversation.emit)
        self._convs.delete_conversation.connect(self.delete_conversation.emit)
        self._convs.folder_mutation_requested.connect(
            self.folder_mutation_requested.emit
        )
        self._convs.bulk_delete_ungrouped_requested.connect(
            self.bulk_delete_ungrouped_requested.emit
        )

        self._model_remote_models: list[dict] = []
        self._model_account_status: dict = {}
        self._model_inventory: list[dict] = []

        self._models = _ModelPanel()
        self._models.model_changed.connect(self._on_model_selected)

        self._settings = _SettingsPanel(self._update_controller)
        self._settings.api_keys_saved.connect(self._on_api_keys_saved)
        self._settings.chatgpt_status_changed.connect(
            self._on_chatgpt_status_changed
        )
        self._models.account_refresh_requested.connect(
            self._settings._probe_chatgpt_account
        )
        self._models.local_models_refresh_requested.connect(
            self._request_local_models_refresh
        )
        self._settings.local_models_refresh_requested.connect(
            self._request_local_models_refresh
        )
        self._ollama_models_ready.connect(self._apply_local_models)
        self._settings.capsule_visibility_changed.connect(
            self.capsule_visibility_changed.emit
        )
        self._settings.wake_word_enabled_changed.connect(
            self.wake_word_enabled_changed.emit
        )
        self._settings.getting_started_requested.connect(
            lambda: self.show_onboarding(automatic=False)
        )
        self._settings.permissions_changed.connect(
            self._refresh_full_access_indicator
        )
        self._permission_epoch_changed.connect(
            self._on_permission_epoch_changed
        )
        try:
            from osia.action_policy import get_default_permission_policy

            self._permission_epoch_listener = self._permission_epoch_changed.emit
            permission_store = get_default_permission_policy().store
            permission_store.add_epoch_listener(self._permission_epoch_listener)
            self.destroyed.connect(
                lambda: permission_store.remove_epoch_listener(
                    self._permission_epoch_listener
                )
            )
        except Exception:
            self._permission_epoch_listener = None
        self._update_controller.state_changed.connect(self._update_banner.apply_state)
        self._update_banner.view_requested.connect(self._show_update_details)
        self._update_banner.install_requested.connect(
            lambda: self._update_controller.install(self)
        )
        self._update_banner.dismiss_requested.connect(self._update_controller.dismiss)
        self._update_banner.apply_state(self._update_controller.state)

        self._osia = _OsiaPanel()

        self._autonomous = _AutonomousPanel()
        self._autonomous.selection_changed.connect(
            self._on_autonomous_model_selected
        )
        self._autonomous.start_requested.connect(
            self.autonomous_start_requested.emit
        )
        self._autonomous.authorize_requested.connect(
            self.autonomous_authorize_requested.emit
        )
        self._autonomous.scope_extension_response.connect(
            self.autonomous_scope_extension_response.emit
        )
        self._autonomous.revoke_requested.connect(
            self.autonomous_revoke_requested.emit
        )
        self._autonomous.interrupt_requested.connect(
            self.autonomous_interrupt_requested.emit
        )
        self._autonomous.abort_requested.connect(
            self.autonomous_abort_requested.emit
        )
        self._refresh_model_inventory()

        self._stack.addWidget(self._chat)
        self._stack.addWidget(self._convs)
        self._stack.addWidget(self._models)
        self._stack.addWidget(self._settings)
        self._stack.addWidget(self._osia)
        self._stack.addWidget(self._autonomous)
        outer.addWidget(self._stack, 1)
        self._active_conversation_id: str | None = None
        self._busy_conversation_ids: set[str] = set()
        self._tool_status_by_conversation: dict[str, str] = {}
        self._onboarding_dialog = None

        # Bottom nav
        nav = QHBoxLayout()
        nav.setSpacing(22)   # WOR-48 P3: 12 → 22, icons spaced further apart
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        self._nav_buttons: dict[int, QPushButton] = {}
        # "OSIA" is a proper noun (tip_key=None) — shown verbatim; the others
        # carry an i18n key so their tooltips re-translate on language change.
        for page_idx, kind, tip_key in [
            (self.PAGE_CONVS,      "conv",       "nav.conversations"),
            (self.PAGE_MODELS,     "models",     "nav.models"),
            (self.PAGE_SETTINGS,   "settings",   "nav.settings"),
            (self.PAGE_OSIA,       "osia",       None),
            (self.PAGE_AUTONOMOUS, "autonomous", "nav.autonomous"),
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

        self._theme.theme_changed.connect(self._on_theme_changed)
        self._apply_style()
        self._refresh_full_access_indicator()
        self._stack.setCurrentIndex(self.PAGE_CHAT)
        QTimer.singleShot(0, self._request_local_models_refresh)

    # ── Window placement ──────────────────────────────────────────────────

    def _show_update_details(self) -> None:
        self.show_settings_category("settings.cat.updates")

    def _refresh_full_access_indicator(self, *_args) -> None:
        try:
            from osia.action_policy import permission_center_snapshot

            snapshot = permission_center_snapshot()
            if snapshot["full_access_enabled"]:
                self._full_access_indicator.setText(
                    i18n.t("permissions.full_with_limits")
                    if snapshot["full_access_with_limits"]
                    else "FULL ACCESS"
                )
                self._full_access_indicator.show()
            else:
                self._full_access_indicator.hide()
        except Exception:
            self._full_access_indicator.hide()

    def _on_permission_epoch_changed(self, _epoch: int) -> None:
        self._refresh_full_access_indicator()
        self._settings._refresh_permissions_summary()

    def show_settings_category(
        self, category_key: str, *, select_chatgpt: bool = False,
    ) -> None:
        """Open one Settings category without the active-nav toggle behavior."""

        self._stack.setCurrentIndex(self.PAGE_SETTINGS)
        button = self._nav_buttons.get(self.PAGE_SETTINGS)
        if button is not None:
            button.setChecked(True)
        self._animate_width(POPUP_W_WIDE)
        if category_key == "settings.cat.models":
            self._settings.show_answer_settings(chatgpt=select_chatgpt)
        elif category_key == "settings.cat.mobile":
            self._settings.show_mobile_settings()
        else:
            self._settings.show_category(category_key)
        if not self.isVisible():
            self._show_animated()

    def show_onboarding(self, *, automatic: bool = False, state_path=None) -> None:
        """Show the native guide; manual reopening never resets configuration."""

        if not _is_deleted(self._onboarding_dialog):
            try:
                if self._onboarding_dialog.isVisible():
                    self._onboarding_dialog.raise_()
                    self._onboarding_dialog.activateWindow()
                    return
            except RuntimeError:
                pass
        from osia.gui.onboarding import OnboardingDialog

        dialog = OnboardingDialog(
            self, automatic=automatic, state_path=state_path,
        )
        dialog.finished.connect(dialog.deleteLater)
        self._onboarding_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def set_anchor(self, widget: QWidget) -> None:
        """Set the widget (the Dynamic Island capsule) the popup opens under.

        The capsule is draggable, so its geometry is re-read on every
        :meth:`toggle` — the popup always opens below wherever the capsule
        currently is.
        """
        self._anchor = widget

    def anchor_below_island(self, island_geom_bottom_y: int = 60) -> None:
        # A still-running width tween would keep writing stale geometry over
        # the position computed here — settle it first.
        self._finish_width_anim()
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()

        # Place the window at the ACTIVE PAGE's width + the fixed height and set
        # its geometry explicitly (not just move()), so reopen maps in-bounds.
        # The DEFINITIVE cure for the Settings-reopen oversize (native window
        # ~800x775 swallowing mouse events around the 640x560 content) is the
        # SetNoConstraint on the outer layout in __init__ — that stops the
        # top-level layout from forcing the window minimum up to the Settings
        # page's cached ~800x700 minimumSize. This setGeometry just supplies the
        # correct in-bounds per-page geometry: WIDE for the Settings master-detail
        # page, COMPACT for every other page; target_h is always the fixed
        # POPUP_H. First-open is unaffected — it opens on PAGE_CHAT (COMPACT) and
        # still snaps 440→640 in-place via _animate_width when the Settings nav is
        # clicked on the mapped window.
        target_w = (POPUP_W_WIDE
                    if self._stack.currentIndex() == self.PAGE_SETTINGS
                    else POPUP_W)
        target_h = POPUP_H

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
            x = island_geo.center().x() - target_w // 2
            y = island_geo.bottom() + 10
            if (y + target_h > geo.y() + geo.height()
                    and island_geo.top() - 10 - target_h >= geo.y()):
                y = island_geo.top() - 10 - target_h
            x = max(geo.x(), min(x, geo.x() + geo.width() - target_w))
            y = max(geo.y(), min(y, geo.y() + geo.height() - target_h))
            self.setGeometry(x, y, target_w, target_h)
            return

        # No anchor — legacy fixed placement at the top-centre of the screen.
        x = geo.x() + (geo.width() - target_w) // 2
        y = geo.y() + max(island_geom_bottom_y, 60)
        self.setGeometry(x, y, target_w, target_h)

    def _animate_width(self, target_w: int) -> None:
        """Resize the window to ``target_w`` (COMPACT or WIDE), instantly.

        WOR-48 audit #2: this used to TWEEN the geometry over several frames, but
        while the 440↔640 tween was mid-flight the settings category nav slid
        under the cursor, so the first category click frequently missed (the
        ~50% settings "freeze"). The width now SNAPS to its final value in a
        single setGeometry, so the window is never a moving target and the first
        click always lands. It still grows/shrinks symmetrically around its
        horizontal centre (so it stays put under the capsule) and is clamped
        on-screen exactly as before — only the animated interpolation is gone.
        """
        if self.width() == target_w:
            return
        self._width_anim.stop()
        cur = self.geometry()
        x = cur.x() + (cur.width() - target_w) // 2
        y = cur.y()
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        x = max(geo.x(), min(x, geo.x() + geo.width() - target_w))
        y = max(geo.y(), min(y, geo.y() + geo.height() - cur.height()))
        self.setGeometry(QRect(x, y, target_w, cur.height()))
        self.update()  # repaint the resized translucent canvas cleanly

    def _finish_width_anim(self) -> None:
        """If the width tween is mid-flight, jump it straight to its end state.

        Called before anything that repositions the window wholesale
        (re-anchoring on toggle, dragging) so a running animation never fights
        the explicit move — or keeps interpolating toward stale coordinates
        after it.
        """
        if self._width_anim.state() == QPropertyAnimation.State.Running:
            end = self._width_anim.endValue()
            self._width_anim.stop()
            if isinstance(end, QRect):
                self.setGeometry(end)

    def toggle(self) -> None:
        if self.isVisible() and not self._hiding:
            self._hide_animated()
        else:
            self._show_animated()

    def _show_animated(self) -> None:
        self._window_opacity_anim.stop()
        self._hiding = False
        self._refresh_full_access_indicator()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.anchor_below_island()
        if reduced_motion_requested(self):
            self.setWindowOpacity(1.0)
            self.show()
        else:
            self.setWindowOpacity(0.0)
            self.show()
            self._window_opacity_anim.setDuration(MOTION.appearance_ms)
            self._window_opacity_anim.setStartValue(0.0)
            self._window_opacity_anim.setEndValue(1.0)
            self._window_opacity_anim.start()
        self.raise_()
        self.activateWindow()

    def _hide_animated(self) -> None:
        if not self.isVisible():
            return
        self._window_opacity_anim.stop()
        if reduced_motion_requested(self):
            self._hiding = False
            self.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, False
            )
            self.hide()
            self.setWindowOpacity(1.0)
            return
        self._hiding = True
        # The fading surface stops intercepting desktop clicks immediately.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._window_opacity_anim.setDuration(MOTION.micro_ms)
        self._window_opacity_anim.setStartValue(self.windowOpacity())
        self._window_opacity_anim.setEndValue(0.0)
        self._window_opacity_anim.start()

    def _on_window_opacity_finished(self) -> None:
        if not self._hiding:
            return
        self._hiding = False
        self.hide()
        self.setWindowOpacity(1.0)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

    # ── Public API ────────────────────────────────────────────────────────

    def show_chat(self) -> None:
        changed = self._stack.currentIndex() != self.PAGE_CHAT
        self._stack.setCurrentIndex(self.PAGE_CHAT)
        if changed:
            self._motion.fade_in(self._chat, MOTION.page_ms)
        for b in self._nav_buttons.values():
            b.setChecked(False)
        self._apply_current_busy()
        # Leaving settings via any "back to chat" path shrinks back to COMPACT.
        self._animate_width(POPUP_W)

    def set_messages(self, messages: list[dict]) -> None:
        self._chat.set_messages(messages)

    def bind_unbound_draft(self, conversation_id: str) -> None:
        self._chat.bind_unbound_draft(conversation_id)

    def accept_draft_submission(self, attachment_ids) -> bool:
        return self._chat.accept_draft_submission(attachment_ids)

    def accept_directive_submission(self, attachment_ids=()) -> bool:
        return self._chat.accept_directive_submission(attachment_ids)

    def show_slash_information(self, payload: dict) -> None:
        """Show safe informational data without appending a chat turn."""
        title = str(payload.get("title") or "OSIA")
        french = i18n.get_language() == "fr"
        labels = {
            "commands": "Commandes" if french else "Commands",
            "families": "Familles" if french else "Families",
            "connectors": "Connecteurs" if french else "Connectors",
            "surface": "Surface",
            "routing_mode": "Routage" if french else "Routing",
            "provider": "Fournisseur" if french else "Provider",
            "model": "Mod\u00e8le" if french else "Model",
            "reasoning": "Raisonnement" if french else "Reasoning",
            "permission_mode": "Autorisation" if french else "Permission",
            "provider_ready": (
                "Fournisseur pr\u00eat" if french else "Provider ready"
            ),
            "connector_count": (
                "Nombre de connecteurs" if french else "Connector count"
            ),
            "transport": "Transport",
        }
        lines = []
        for key, value in payload.items():
            if key == "title":
                continue
            label = labels.get(key, key.replace("_", " ").title())
            if isinstance(value, list):
                rendered = []
                for item in value:
                    if isinstance(item, dict):
                        if key == "commands":
                            hint = str(item.get("argument_hint") or "")
                            alias = str(item.get("alias") or "")
                            rendered.append(
                                f"{alias}{f' <{hint}>' if hint else ''} \u2014 "
                                f"{str(item.get('description') or '')}"
                            )
                        else:
                            rendered.append(
                                " · ".join(str(v) for v in item.values())
                            )
                    else:
                        rendered.append(str(item))
                value = "\n".join(rendered)
            elif isinstance(value, bool):
                value = (
                    "oui" if value and french else
                    "non" if french else
                    "yes" if value else "no"
                )
            lines.append(f"{label}: {value}")
        QMessageBox.information(self, title, "\n\n".join(lines))

    def show_attachment_error(self, error: DesktopAttachmentError) -> None:
        self._chat.show_attachment_error(error)

    def discard_conversation_draft(self, conversation_id: str | None) -> None:
        self._chat.discard_draft(conversation_id)

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

    def append_assistant_stream_chunk(self, text: str) -> None:
        self._chat.append_assistant_stream_chunk(text)

    def finalize_assistant_stream(self, text: str) -> None:
        self._chat.finalize_assistant_stream(text)

    def fail_assistant_stream(self, text: str) -> None:
        self._chat.fail_assistant_stream(text)

    def cancel_assistant_stream(self, *, flush: bool = True) -> None:
        self._chat.cancel_assistant_stream(flush=flush)

    def refresh_conversations(self, active_id: str | None) -> None:
        if active_id != self._active_conversation_id:
            self._chat.set_draft_context(active_id)
        self._active_conversation_id = active_id
        self._convs.set_active(active_id)
        self._apply_current_busy()

    def set_conversation_busy(self, conv_id: str, busy: bool) -> None:
        if not conv_id:
            return
        if busy:
            self._busy_conversation_ids.add(conv_id)
        else:
            self._busy_conversation_ids.discard(conv_id)
            self._tool_status_by_conversation.pop(conv_id, None)
        self._apply_current_busy()

    def set_conversation_tool_status(self, conv_id: str, status: str) -> None:
        if not conv_id:
            return
        self._tool_status_by_conversation[conv_id] = str(status or "")
        self._busy_conversation_ids.add(conv_id)
        self._apply_current_busy()

    def show_conversation_busy_notice(self) -> None:
        self._chat.show_notice(_ChatPage._BUSY_TEXT)

    def set_current_model(
        self,
        model_id: str | None,
        provider: str | None = None,
    ) -> None:
        self._models.set_current(model_id, provider)

    def set_remote_models(self, remote: list) -> None:
        self._model_remote_models = [
            dict(item) for item in remote or [] if isinstance(item, dict)
        ]
        self._settings.set_remote_models(remote)
        self._refresh_model_inventory()

    def _on_api_keys_saved(self, keys: dict) -> None:
        self._refresh_model_inventory(keys)
        self.api_keys_saved.emit(dict(keys or {}))

    def _on_chatgpt_status_changed(self, status: dict) -> None:
        self._model_account_status = dict(status or {})
        # Kept for the focused account-panel contract tests and selection side
        # effect; rendering itself consumes only the canonical inventory below.
        self._models._account_status = dict(status or {})
        self._refresh_model_inventory()
        self.chatgpt_status_changed.emit(dict(status or {}))

    def _refresh_model_inventory(self, keys: dict | None = None) -> None:
        """Rebuild one non-blocking cached inventory and distribute it."""
        try:
            raw_models = list(get_all_models(
                remote_models=self._model_remote_models
            ))
        except Exception:
            raw_models = []
        try:
            key_snapshot = dict(keys) if isinstance(keys, dict) else load_api_keys()
            configured = configured_api_providers(key_snapshot)
        except Exception:
            configured = set()
        self._model_inventory = build_model_inventory(
            raw_models,
            configured,
            self._model_account_status,
        )
        settings = load_settings() or {}
        resolution = resolve_autonomous_selection(
            self._model_inventory,
            autonomous_provider=settings.get("autonomous_model_provider") or "",
            autonomous_model=settings.get("autonomous_model") or "",
            main_provider=settings.get("selected_model_provider") or "",
            main_model=settings.get("selected_model") or "",
        )
        account_status_pending = not bool(self._model_account_status)
        # Until the first account snapshot arrives, a missing entry may still be
        # a valid account model (including a legacy bare id). Render the safe
        # fallback, but do not destroy the saved identity before that probe.
        defer_account_fallback = bool(
            account_status_pending and resolution.get("fallback_occurred")
        )
        if resolution.get("entry") and not defer_account_fallback:
            provider_id = resolution["provider_id"]
            model_id = resolution["model_id"]
            if settings.get("autonomous_model_provider") != provider_id:
                set_setting("autonomous_model_provider", provider_id)
            if settings.get("autonomous_model") != model_id:
                set_setting("autonomous_model", model_id)
        self._models.set_inventory(self._model_inventory)
        self._autonomous.set_inventory(self._model_inventory, resolution)

    def _on_autonomous_model_selected(self, provider: str, model: str) -> None:
        settings = load_settings() or {}
        if settings.get("autonomous_model_provider") != provider:
            set_setting("autonomous_model_provider", provider)
        if settings.get("autonomous_model") != model:
            set_setting("autonomous_model", model)

    def _request_local_models_refresh(self) -> None:
        refresh_ollama_models_async(self._publish_local_models)

    def _publish_local_models(self, models: list) -> None:
        self._ollama_models_ready.emit(list(models))

    def _apply_local_models(self, _models: list) -> None:
        self._settings._apply_local_models_snapshot()
        self._refresh_model_inventory()

    # ── Autonomous agent (WOR-45 Phase 4) ─────────────────────────────────

    def show_autonomous(self, state) -> None:
        """Switch to the autonomous page and seed it from a ``task_state`` dict
        (used by the host when resuming an interrupted task). Surfaces the popup
        if it is hidden so a resumed task is actually visible."""
        self._stack.setCurrentIndex(self.PAGE_AUTONOMOUS)
        btn = self._nav_buttons.get(self.PAGE_AUTONOMOUS)
        if btn is not None:
            btn.setChecked(True)
        self._animate_width(POPUP_W)   # autonomous page uses the COMPACT width
        self._settings._probe_chatgpt_account()
        self._request_local_models_refresh()
        self._autonomous.seed_from_state(state)
        if not self.isVisible():
            self._show_animated()

    def update_autonomous_progress(self, payload) -> None:
        """Forward a progress snapshot (already on the GUI thread) to the panel."""
        self._autonomous.apply_progress(payload)

    # ── Internal handlers ─────────────────────────────────────────────────

    def _retranslate_nav(self) -> None:
        for btn in self._nav_buttons.values():
            tip_key = getattr(btn, "_tip_key", None)
            if tip_key:
                btn.setToolTip(i18n.t(tip_key))

    def _apply_current_busy(self) -> None:
        active_id = self._active_conversation_id
        self._chat.set_busy(
            bool(active_id and active_id in self._busy_conversation_ids)
        )
        if active_id:
            self._chat.set_tool_status(
                self._tool_status_by_conversation.get(active_id, "")
            )

    def _on_nav_clicked(self, page_idx: int) -> None:
        with perf_timer(f"nav -> page {page_idx}"):
            # Clicking the active nav button returns to the chat.
            if self._stack.currentIndex() == page_idx:
                self.show_chat()
                return
            self._stack.setCurrentIndex(page_idx)
            page = self._stack.currentWidget()
            if page is not None:
                self._motion.fade_in(page, MOTION.page_ms)
            # WOR-48 P3: settings hosts the master-detail layout and needs the
            # WIDE window; every other page uses the COMPACT width.
            self._animate_width(
                POPUP_W_WIDE if page_idx == self.PAGE_SETTINGS else POPUP_W
            )
            if page_idx == self.PAGE_CONVS:
                self._convs.refresh()
            elif page_idx == self.PAGE_OSIA:
                self._osia.refresh()
            elif page_idx == self.PAGE_AUTONOMOUS:
                self._settings._probe_chatgpt_account()
                self._request_local_models_refresh()

    def _on_new_conv(self) -> None:
        self.new_conversation.emit()
        self.show_chat()

    def _on_open_conv(self, conv_id: str) -> None:
        self.open_conversation.emit(conv_id)
        self.show_chat()

    def _on_model_selected(self, model_id: str, provider: str) -> None:
        self.model_changed.emit(model_id, provider)
        # Show the new selection state immediately.
        self._models.set_current(model_id, provider)

    # ── Dragging (WOR-74) ─────────────────────────────────────────────────
    # Buttons, inputs and list rows accept their own mouse presses, so any
    # press that reaches the popup itself is on an "empty" area — dragging
    # there moves the whole window. The stacked pages have no drag handlers of
    # their own: an empty-area press propagates up to here, so the window is
    # dragged the same way from every page.
    #
    # The move is delegated to the OS/compositor via QWindow.startSystemMove()
    # — the popup NEVER moves or repaints itself during a drag. The previous
    # implementation moved the window by hand (self.move() + repaint) on every
    # mouse-move; on this frameless + WA_TranslucentBackground + always-on-top
    # window that flooded the GUI thread and ultimately crashed Qt's C++ layer
    # (Windows fatal access violation 0xC0000005 in mouseMoveEvent), worst at
    # the 640px Settings width. startSystemMove hands the ENTIRE drag to the OS:
    # zero per-event move()/update() on the GUI thread → no repaint flood, no
    # capsule expose storm, no crash. There is therefore no mouseMoveEvent /
    # mouseReleaseEvent override and no Python-side drag state to track or clean
    # up — the OS ends the move on button release by itself.

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Don't let an in-flight width tween fight the drag — settle it
            # before handing the move to the OS.
            self._finish_width_anim()
            # WOR-74: delegate the drag to the OS instead of moving the window
            # by hand. Guard windowHandle(): it is None until the native window
            # exists (the popup is always shown before it can be dragged, so in
            # practice it is non-None here).
            wh = self.windowHandle()
            if wh is not None:
                wh.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)

    # ── Painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Wipe the entire widget (corners + shadow margin) to fully
        # transparent each frame before repainting. Without this, the
        # semi-transparent background wash accumulates residue
        # on the translucent window (ghost rectangles, drag smear, and a
        # stale settings detail page that swallows the first category click).
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # The card sits inside the SHADOW_MARGIN band; on non-Windows the band
        # holds the hand-painted drop shadow drawn just below. Inset by
        # SHADOW_MARGIN and -1px so the 1px border stays crisp inside the widget.
        card = self.rect().adjusted(
            SHADOW_MARGIN, SHADOW_MARGIN, -SHADOW_MARGIN - 1, -SHADOW_MARGIN - 1
        )

        # Manual soft drop shadow — replaces the removed QGraphicsDropShadowEffect.
        # Several concentric rounded-rect OUTLINES expand outward from the card and
        # fade to transparent, offset down so the light reads from above. Stroked
        # (not filled) so the translucent card body is never darkened. It is drawn
        # into the backing store, which the transparent clear above wipes every
        # frame → moving/resizing leaves no ghost rectangles.
        #
        # WOR-74: the window is dragged via the OS (startSystemMove), which moves
        # the existing backing store — paintEvent no longer fires per mouse-move
        # during a drag, so there is no repaint flood to optimise against and the
        # old drag-time shadow-skip branch is gone. WOR-237 disables only these
        # external rings on Windows; other platforms keep the established paint.
        # The transparent clear at the top still wipes the entire widget rect.
        if _PAINT_EXTERNAL_WINDOW_SHADOW:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(SHADOW_LAYERS, 0, -1):
                frac = (i - 1) / (SHADOW_LAYERS - 1)   # 0 = inner, 1 = outer
                alpha = int(round(SHADOW_ALPHA * (1.0 - frac)))
                spread = (i - 1) * SHADOW_STEP
                pen = QPen(QColor(0, 0, 0, alpha))
                pen.setWidthF(2.0)
                painter.setPen(pen)
                ring = QRectF(
                    card.x() + 0.5 - spread,
                    card.y() + 0.5 - spread + SHADOW_DY,
                    card.width() + spread * 2,
                    card.height() + spread * 2,
                )
                ring_path = QPainterPath()
                ring_path.addRoundedRect(ring, RADIUS + spread, RADIUS + spread)
                painter.drawPath(ring_path)

        # The glass card itself, painted ON TOP of the shadow.
        path = QPainterPath()
        path.addRoundedRect(
            card.x() + 0.5, card.y() + 0.5,
            card.width(), card.height(),
            RADIUS, RADIUS,
        )
        palette = self._theme.palette
        painter.fillPath(path, palette.color("background", 125))
        # A second, slightly lighter wash layered on top fakes a frosted-glass
        # blur — the panel reads as semi-transparent glass rather than a flat
        # dark fill.
        painter.fillPath(path, palette.color("glass", 85))
        painter.setPen(palette.color("primary", 70))
        painter.drawPath(path)
        painter.end()

    # ── Style ─────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        self.setStyleSheet(themed_stylesheet("""
            QWidget {
                color: #e8e6f0;
                font-family: -apple-system, "Segoe UI", "SF Pro Text",
                             "Helvetica Neue", sans-serif;
            }

            /* Shared state affordances never change a widget's box metrics. */
            QPushButton:focus {
                outline: none;
                border-color: rgba(196, 181, 245, 0.72);
            }
            QPushButton:pressed {
                color: #e8e6f0;
            }

            /* WOR-48 P3: square close button with lightly rounded corners
               (was a 13px-radius circle); subtle hover kept. */
            QPushButton#closeBtn {
                background: rgba(196, 181, 245, 0.06);
                color: rgba(232, 230, 240, 0.7);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 8px;
                font-size: 13px;
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
            QLabel#themeDot {
                background: #c4b5f5;
                border-radius: 4px;
            }
            QLabel#fullAccessIndicator {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #c4b5f5;
                border-radius: 5px;
                padding: 4px 7px;
                font-size: 10px;
                font-weight: 700;
            }
            QFrame#updateBanner {
                background: rgba(196, 181, 245, 0.12);
                border: 1px solid rgba(196, 181, 245, 0.34);
                border-radius: 10px;
            }
            QFrame#majorUpdateBanner {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(139, 92, 255, 0.22),
                    stop:0.52 rgba(88, 86, 170, 0.13),
                    stop:1 rgba(52, 216, 255, 0.18));
                border: 1px solid rgba(88, 205, 255, 0.52);
                border-radius: 10px;
            }
            QLabel#updateBannerText {
                color: #f8f6ff;
                font-size: 11px;
                font-weight: 600;
            }
            QProgressBar#updateBannerProgress {
                min-height: 4px;
                max-height: 4px;
                background: rgba(196, 181, 245, 0.16);
                border: none;
                border-radius: 2px;
            }
            QProgressBar#updateBannerProgress::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #8b5cff, stop:1 #34d8ff);
                border-radius: 2px;
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

            /* WOR-48 P2 — settings category nav (master-detail). Transparent
               list; selected row is a violet accent pill in the existing
               rgba(196,181,245,…) palette, hover is a subtler wash. */
            QListWidget#settingsNav {
                background: transparent;
                border: none;
                outline: 0;
                font-size: 12px;
            }
            QListWidget#settingsNav::item {
                color: #9d9aad;
                padding: 7px 8px;
                margin: 2px 1px;
                border-radius: 9px;
            }
            QListWidget#settingsNav::item:hover {
                background: rgba(196, 181, 245, 0.08);
                color: #e8e6f0;
            }
            QListWidget#settingsNav::item:selected {
                background: rgba(196, 181, 245, 0.16);
                color: #e8e6f0;
            }

            /* Chat ───────────────────────────────────────────────────── */
            QLabel#chatEmpty {
                color: #9d9aad;
                font-size: 12px;
                padding: 40px 12px;
            }
            QFrame#msgBubble {
                border-radius: 14px;
                padding: 0px;
            }
            /* WOR-48: softer bubble borders for a lighter, glassier look. */
            QFrame#msgBubble[role="user"] {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.22);
            }
            QFrame#msgBubble[role="assistant"] {
                background: rgba(15, 13, 22, 0.62);
                border: 1px solid rgba(196, 181, 245, 0.08);
            }
            QFrame#msgBubble[streaming="true"] {
                border: 1px solid rgba(196, 181, 245, 0.22);
            }
            QFrame#msgBubble[error="true"] {
                border: 1px solid rgba(220, 80, 80, 0.55);
            }
            QLabel#msgText, QTextBrowser#msgText {
                color: #e8e6f0;
                font-size: 12px;
                background: transparent;
                border: none;
                padding: 0px;
            }

            /* WOR-48: lighter input frame — translucent fill, softer border. */
            QFrame#inputWrap {
                background: rgba(19, 17, 26, 0.62);
                border: 1px solid rgba(196, 181, 245, 0.10);
                border-radius: 14px;
            }
            QFrame#inputWrap:hover {
                border: 1px solid rgba(196, 181, 245, 0.26);
            }
            QTextEdit#chatInput {
                background: transparent;
                border: none;
                color: #e8e6f0;
                font-size: 12px;
                padding: 4px 2px;
            }
            QPushButton#iconBtn, QPushButton#sendBtn {
                border: 1px solid rgba(196, 181, 245, 0.18);
                border-radius: 8px;
                padding: 0px;
            }
            QPushButton#iconBtn {
                background: rgba(196, 181, 245, 0.06);
                color: rgba(232, 230, 240, 0.8);
            }
            QPushButton#iconBtn:hover {
                background: rgba(196, 181, 245, 0.16);
                border-color: rgba(196, 181, 245, 0.42);
                color: #e8e6f0;
            }
            QPushButton#iconBtn:pressed {
                background: rgba(196, 181, 245, 0.10);
                border-color: rgba(196, 181, 245, 0.58);
            }
            QPushButton#sendBtn {
                background: rgba(196, 181, 245, 0.85);
                border-color: rgba(196, 181, 245, 0.85);
                color: #0d0b12;
            }
            QPushButton#sendBtn:hover {
                background: rgba(196, 181, 245, 1.0);
                border-color: rgba(196, 181, 245, 1.0);
            }
            QPushButton#sendBtn:pressed {
                background: rgba(196, 181, 245, 0.70);
                border-color: rgba(196, 181, 245, 0.70);
                color: #0d0b12;
            }
            QPushButton#iconBtn:focus, QPushButton#sendBtn:focus {
                border: 2px solid rgba(196, 181, 245, 0.72);
            }
            QTextEdit#chatInput:focus {
                background: rgba(196, 181, 245, 0.06);
                border-radius: 6px;
            }

            /* Disabled composer controls stay visually inactive. */
            QPushButton#iconBtn:disabled, QPushButton#sendBtn:disabled {
                background: rgba(133, 130, 145, 0.14);
                border: 1px solid rgba(133, 130, 145, 0.32);
                color: #858291;
            }
            QTextEdit#chatInput:disabled {
                color: #858291;
            }
            QLabel#chatNotice {
                color: rgba(232, 230, 240, 0.72);
                font-size: 11px;
                padding: 0 2px 2px 2px;
            }
            QScrollArea#attachmentScroll, QWidget#attachmentHost {
                background: transparent;
                border: none;
            }
            QFrame#attachmentChip {
                background: rgba(196, 181, 245, 0.07);
                border: 1px solid rgba(196, 181, 245, 0.20);
                border-radius: 8px;
            }
            QFrame#attachmentChip[validation="error"] {
                border: 1px solid rgba(220, 100, 100, 0.72);
                background: rgba(120, 45, 55, 0.16);
            }
            QLabel#attachmentKind {
                color: #c4b5f5;
                font-size: 8px;
                font-weight: 700;
                background: transparent;
            }
            QLabel#attachmentName {
                color: #e8e6f0;
                font-size: 11px;
                background: transparent;
            }
            QLabel#attachmentSize {
                color: rgba(232, 230, 240, 0.58);
                font-size: 9px;
                background: transparent;
            }
            QPushButton#attachmentRemove {
                color: rgba(232, 230, 240, 0.72);
                background: transparent;
                border: none;
                border-radius: 6px;
                padding: 0px;
                font-size: 16px;
            }
            QPushButton#attachmentRemove:hover,
            QPushButton#attachmentRemove:focus {
                color: #e8e6f0;
                background: rgba(196, 181, 245, 0.18);
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
            QFrame#folderRow {
                background: rgba(196, 181, 245, 0.10);
                border: 1px solid rgba(196, 181, 245, 0.28);
                border-radius: 10px;
            }
            QLabel#folderTitle {
                font-size: 12px;
                font-weight: 700;
                color: #e8e6f0;
            }
            QLabel#folderCount, QLabel#folderSectionLabel {
                font-size: 10px;
                color: #9d9aad;
            }
            QPushButton#folderToggle, QPushButton#folderMenu,
            QPushButton#convMore, QPushButton#convRename {
                background: rgba(196, 181, 245, 0.08);
                border: 1px solid rgba(196, 181, 245, 0.16);
                border-radius: 8px;
                color: #e8e6f0;
            }
            QPushButton#folderToggle:hover, QPushButton#folderMenu:hover,
            QPushButton#convMore:hover, QPushButton#convRename:hover {
                background: rgba(196, 181, 245, 0.22);
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
            QLabel#modelReasoningChip {
                color: rgba(196, 181, 245, 0.82);
                font-size: 10px;
                padding-top: 2px;
            }
            QPushButton#modelReasoning {
                background: rgba(196, 181, 245, 0.06);
                border: 1px solid rgba(196, 181, 245, 0.16);
                color: rgba(232, 230, 240, 0.68);
                border-radius: 10px;
                font-size: 13px;
                padding: 0;
            }
            QPushButton#modelReasoning:hover {
                background: rgba(196, 181, 245, 0.14);
                border: 1px solid rgba(196, 181, 245, 0.34);
                color: #e8e6f0;
            }
            QPushButton#modelReasoning[selected="true"] {
                background: rgba(196, 181, 245, 0.16);
                border: 1px solid rgba(196, 181, 245, 0.44);
                color: #c4b5f5;
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
            QDialog#onboardingDialog {
                background: #0d0b12;
                color: #e8e6f0;
            }
            QStackedWidget#onboardingStack, QFrame#historyHost {
                background: transparent;
                border: none;
            }
            QFrame#releaseCard {
                background: #13111a;
                border: 1px solid rgba(196, 181, 245, 0.16);
                border-radius: 10px;
            }
            QFrame#majorUpdateCard {
                background: transparent;
                border: none;
                border-radius: 11px;
            }
            QScrollArea#releaseNotesScroll,
            QScrollArea#releaseNotesScroll > QWidget,
            QScrollArea#releaseNotesScroll > QWidget > QWidget {
                background: transparent;
                border: none;
            }
            QLabel#releaseVersion {
                color: #e8e6f0;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#releaseBadge {
                background: rgba(196, 181, 245, 0.16);
                border: 1px solid rgba(196, 181, 245, 0.34);
                border-radius: 8px;
                color: #c4b5f5;
                padding: 2px 7px;
                font-size: 10px;
            }
            QPushButton#themeChoice {
                background: rgba(196, 181, 245, 0.04);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 9px;
                padding: 6px 8px;
                color: #e8e6f0;
                font-size: 11px;
                text-align: left;
            }
            QPushButton#themeChoice:hover, QPushButton#themeChoice:focus {
                background: rgba(196, 181, 245, 0.12);
                border: 1px solid rgba(196, 181, 245, 0.38);
            }
            QPushButton#themeChoice:checked {
                background: rgba(196, 181, 245, 0.20);
                border: 1px solid rgba(196, 181, 245, 0.62);
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
            QLabel#sectionDesc {
                font-size: 11px;
                color: #8a8798;
                line-height: 1.35;
            }
            QTableWidget#permissionsTable {
                background: rgba(19, 17, 26, 0.62);
                alternate-background-color: rgba(196, 181, 245, 0.04);
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                color: #e8e6f0;
                gridline-color: rgba(196, 181, 245, 0.10);
                selection-background-color: rgba(196, 181, 245, 0.22);
            }
            QTableWidget#permissionsTable QHeaderView::section,
            QTableWidget#permissionsTable QTableCornerButton::section {
                background: rgba(196, 181, 245, 0.10);
                border: none;
                border-right: 1px solid rgba(196, 181, 245, 0.10);
                border-bottom: 1px solid rgba(196, 181, 245, 0.12);
                color: #e8e6f0;
                padding: 5px;
            }
            QSpinBox#permissionsAmount {
                background: #13111a;
                border: 1px solid rgba(196, 181, 245, 0.12);
                border-radius: 10px;
                color: #e8e6f0;
                padding: 6px 10px;
            }
            QSpinBox#permissionsAmount:focus {
                border: 1px solid rgba(196, 181, 245, 0.55);
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
            QLabel#visionHint {
                font-size: 11px;
                color: #9d9aad;
                font-style: italic;
            }
            QLabel#infoBadge {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid #c4b5f5;
                border-radius: 7px;
                color: #c4b5f5;
                font-size: 9px;
                font-weight: 700;
            }
            QPushButton#infoBadge {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid #c4b5f5;
                border-radius: 9px;
                color: #c4b5f5;
                padding: 0px;
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton#infoBadge:hover, QPushButton#infoBadge:focus {
                background: rgba(196, 181, 245, 0.30);
                border-color: #e8e6f0;
            }

            /* Mobile token + reveal dialog ──────────────────────────────── */
            QLabel#tokenMask {
                font-family: "Consolas", "Cascadia Mono", "DejaVu Sans Mono", "SF Mono", monospace;
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
            QLabel#comingSoonPill {
                background: rgba(232, 230, 240, 0.06);
                border: 1px solid rgba(232, 230, 240, 0.14);
                border-radius: 9px;
                padding: 3px 8px;
                color: rgba(232, 230, 240, 0.58);
                font-size: 10px;
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
                padding: 6px 12px;          /* WOR-48: trimmed pill padding */
                color: #e8e6f0;
                font-size: 12px;
                text-align: left;
            }
            QPushButton#convPick:hover {
                background: rgba(196, 181, 245, 0.18);
                border: 1px solid rgba(196, 181, 245, 0.45);
                color: #e8e6f0;
            }

            QPushButton#memoryDangerAction {
                background: transparent;
                border: none;
                color: rgba(232, 230, 240, 0.62);
                border-radius: 0px;
                padding: 4px 6px;
                font-size: 11px;
            }
            QPushButton#memoryDangerAction:hover {
                background: transparent;
                border: none;
                color: #ff6b6b;
            }
            QPushButton#memoryDangerAction:disabled {
                background: transparent;
                border: none;
                color: #858291;
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
            QPushButton#destructiveSecondaryBtn {
                background: rgba(220, 80, 80, 0.06);
                border: 1px solid rgba(220, 80, 80, 0.28);
                color: #e0a3a3;
                border-radius: 10px;
                padding: 8px 16px;
                font-size: 12px;
            }
            QPushButton#destructiveSecondaryBtn:hover,
            QPushButton#destructiveSecondaryBtn:focus {
                background: rgba(220, 80, 80, 0.14);
                border-color: rgba(220, 80, 80, 0.58);
            }
            QPushButton#destructiveSecondaryBtn:disabled {
                background: transparent;
                border-color: rgba(133, 130, 145, 0.24);
                color: #858291;
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
                font-family: "Consolas", "Cascadia Mono", "DejaVu Sans Mono", "SF Mono", monospace;
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

            /* WOR-48: scrollbars are fully hidden everywhere (the per-area
               ScrollBarAlwaysOff policy does the hiding). This QSS is a
               belt-and-suspenders fallback so no bar ever renders — including
               in child dialogs that don't set the policy: zero width and a
               fully transparent track + handle. Scrolling stays functional
               (wheel/drag/trackpad). */
            QScrollBar:vertical {
                background: transparent;
                width: 0px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: transparent;
                border-radius: 0px;
                min-height: 0px;
            }
            QScrollBar::handle:vertical:hover {
                background: transparent;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
        """, self._theme.palette))

    def _on_theme_changed(self, _key: str) -> None:
        self._apply_style()
        for nav_button in self._nav_buttons.values():
            nav_button.update()
        self.update()

    # ── Keys ──────────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._hide_animated()
            return
        super().keyPressEvent(event)


# ── Self-test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import sys

    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

    app = QApplication(sys.argv)
    p = MainPopup()
    p.anchor_below_island()
    p.show()
    p.append_message("user", "Bonjour OSIA")
    p.append_message("assistant", "Salut ! Comment puis-je t'aider ?")
    sys.exit(app.exec())
