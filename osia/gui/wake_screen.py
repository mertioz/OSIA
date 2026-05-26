#!/usr/bin/env python3
"""
OSIA Wake Screen — full-screen transparent overlay with animated pulsing dots.

Companion widgets (model selector, settings button, settings panel) live in
this module too. They share a dark glassmorphism look, and the model selector
and settings button fade in and out together with the wake screen.
"""

import math
from PyQt6.QtWidgets import (
    QWidget, QComboBox, QApplication, QPushButton, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QScrollArea, QFrame, QCheckBox, QGraphicsDropShadowEffect,
)
from PyQt6.QtCore import (
    Qt, QTimer, QPointF, pyqtSignal, QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import QPainter, QColor

from osia.models import (
    PROVIDERS, API_PROVIDERS, HARDCODED_MODELS, get_all_models,
    discover_models_async,
)
from osia.config import (
    load_api_keys, save_api_keys, load_settings, set_setting, set_api_key,
)
from osia.utils.systemd_manager import (
    is_service_enabled, enable_service, disable_service, get_service_status,
)


STARTUP_SERVICES = ("osia-server.service", "osia-tunnel.service")


# ── Fade helper ──────────────────────────────────────────────────────────

class FadeWindow(QWidget):
    """Mixin-ish base for top-level windows that fade in/out on show/hide.

    Subclasses (or callers) use ``fade_in()`` and ``fade_out()`` instead of
    ``show()`` / ``hide()`` to get a 300ms windowOpacity animation.
    """

    FADE_MS = 300

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(self.FADE_MS)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_anim.finished.connect(self._on_fade_finished)
        self._fading_out = False

    def fade_in(self):
        self._fading_out = False
        self._fade_anim.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def fade_out(self):
        if not self.isVisible():
            return
        self._fading_out = True
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _on_fade_finished(self):
        if self._fading_out:
            self.hide()
            self._fading_out = False


# ── Wake screen ──────────────────────────────────────────────────────────

class WakeScreen(FadeWindow):
    """Full-screen semi-transparent overlay with animated pulsing dots."""

    fade_in_started = pyqtSignal()
    fade_out_started = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        screen = self.screen()
        if screen:
            self.setGeometry(screen.geometry())
        else:
            self.setGeometry(0, 0, 1920, 1080)

        self._angle = 0.0
        self._pulse_phase = 0.0
        self._dot_radius = 6
        self._orbit_radius = 30
        self._dot_color = (255, 255, 255)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.setInterval(16)  # ~60fps

        self.setWindowOpacity(0.0)
        self.hide()

    def show_wake(self):
        """Show with fade-in and start the dot animation."""
        self._angle = 0.0
        self._pulse_phase = 0.0
        self._timer.start()
        # Show the wake overlay FIRST so the companions (connected to
        # fade_in_started) end up above it in the window stack and can
        # receive clicks. The overlay itself is WA_TransparentForMouseEvents.
        self.fade_in()
        self.fade_in_started.emit()

    def hide_wake(self):
        """Fade out and stop the animation when fully hidden."""
        self.fade_out_started.emit()
        self.fade_out()

    def _on_fade_finished(self):
        super()._on_fade_finished()
        if not self.isVisible():
            self._timer.stop()

    def set_state(self, state: str):
        """'listening' -> white dots, 'thinking' -> violet dots."""
        if state == "listening":
            self._dot_color = (255, 255, 255)
        elif state == "thinking":
            self._dot_color = (196, 181, 245)  # #c4b5f5
        self.update()

    def _animate(self):
        self._angle += 0.06
        self._pulse_phase += 0.08
        if self._angle > 2 * math.pi:
            self._angle -= 2 * math.pi
        if self._pulse_phase > 2 * math.pi:
            self._pulse_phase -= 2 * math.pi
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setBrush(QColor(0, 0, 0, 120))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())

        center = QPointF(self.width() / 2, self.height() / 2)
        pulse = 1.0 + 0.5 * math.sin(self._pulse_phase)
        cr, cg, cb = self._dot_color

        for i in range(3):
            phase = self._angle + (i * 2.0 * math.pi / 3.0)
            x = center.x() + self._orbit_radius * math.cos(phase)
            y = center.y() + self._orbit_radius * math.sin(phase)

            dot_pulse = pulse * (0.8 + 0.2 * math.sin(self._pulse_phase + i))
            r = self._dot_radius * dot_pulse
            alpha = int(180 + 75 * math.sin(self._pulse_phase + i))

            painter.setBrush(QColor(cr, cg, cb, max(0, min(255, alpha))))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(x, y), r, r)

        painter.end()


# ── Model selector ──────────────────────────────────────────────────────

class ModelSelector(FadeWindow):
    """Bottom-right model dropdown, fades in/out with the wake screen.

    Lists all known models — hardcoded API models, locally-installed Ollama
    models, and any models discovered remotely via the providers' APIs.
    """

    model_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        self.setWindowTitle("OSIA Model")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Must receive clicks — WakeScreen above is transparent for events,
        # but this widget is not.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.setFixedSize(220, 36)

        self._combo = QComboBox(self)
        self._combo.setGeometry(0, 0, 220, 36)
        self._combo.setMinimumHeight(36)
        font = self._combo.font()
        font.setPixelSize(11)
        self._combo.setFont(font)

        self._populate()

        self._combo.currentTextChanged.connect(self._on_change)

        self._combo.setStyleSheet("""
            QComboBox {
                background: rgba(15, 15, 15, 200);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid rgba(255, 255, 255, 0.5);
                margin-right: 6px;
            }
            QComboBox QAbstractItemView {
                background: rgba(20, 20, 20, 240);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                selection-background-color: rgba(255, 255, 255, 0.15);
                font-size: 11px;
            }
        """)

        self.setWindowOpacity(0.0)
        self.hide()

    def _populate(self, remote_models=None):
        """Fill the combo with all models, preserving the current selection."""
        current = self._combo.currentText() if self._combo.count() else None
        self._combo.blockSignals(True)
        self._combo.clear()

        models = get_all_models(remote_models=remote_models)
        # Group by provider, with a small header label per group.
        by_provider = {}
        for m in models:
            by_provider.setdefault(m["provider"], []).append(m)

        order = ["anthropic", "openai", "deepseek", "google", "xai", "ollama"]
        for p in order:
            for m in by_provider.get(p, []):
                self._combo.addItem(m["label"], m["id"])

        # Restore selection if possible
        if current:
            idx = self._combo.findText(current)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
        self._combo.blockSignals(False)

    def refresh_models(self, remote_models=None):
        """Re-populate, called after remote model discovery completes."""
        self._populate(remote_models=remote_models)

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.x() + geo.width() - 240, geo.y() + geo.height() - 60)
        # WakeScreen is also stays-on-top; raise above it to receive clicks.
        self.raise_()

    def _on_change(self, label: str):
        if label:
            data = self._combo.currentData()
            self.model_changed.emit(data or label)

    def current_model(self) -> str:
        return self._combo.currentData() or self._combo.currentText()

    def set_current_model(self, model_id: str):
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == model_id:
                self._combo.setCurrentIndex(i)
                return


# ── Settings gear button ────────────────────────────────────────────────

class SettingsButton(FadeWindow):
    """Bottom-left gear button, fades in/out with the wake screen."""

    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.setWindowTitle("OSIA Settings")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Must receive clicks — WakeScreen above is transparent for events,
        # but this widget is not.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.setFixedSize(44, 44)

        self._btn = QPushButton("⚙", self)  # ⚙
        self._btn.setGeometry(0, 0, 44, 44)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self.clicked.emit)

        self._btn.setStyleSheet("""
            QPushButton {
                background: rgba(15, 15, 15, 200);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 22px;
                font-size: 22px;
            }
            QPushButton:hover {
                background: rgba(35, 35, 35, 220);
                border: 1px solid rgba(255, 255, 255, 0.18);
            }
            QPushButton:pressed {
                background: rgba(50, 50, 50, 230);
            }
        """)

        self.setWindowOpacity(0.0)
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.x() + 20, geo.y() + geo.height() - 64)
        # WakeScreen is also stays-on-top; raise above it to receive clicks.
        self.raise_()


# ── Conversation controls (new / delete) ────────────────────────────────

class ConversationControls(FadeWindow):
    """Top-center conversation management bar with two buttons:
    'Nouvelle conversation' and 'Supprimer la conversation'.

    Fades in/out together with the wake screen and matches the existing
    dark glassmorphism look with a violet accent for the conversation
    actions, so it reads as a distinct group from the model selector /
    settings gear.
    """

    new_conversation = pyqtSignal()
    delete_conversation = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.setWindowTitle("OSIA Conversation")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Must receive clicks — WakeScreen above is transparent for events,
        # but this widget is not.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.setFixedSize(380, 40)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self._new_btn = QPushButton("✚  Nouvelle conversation", self)
        self._new_btn.setObjectName("newConvBtn")
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self.new_conversation.emit)

        self._delete_btn = QPushButton("🗑  Supprimer", self)
        self._delete_btn.setObjectName("deleteConvBtn")
        self._delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._delete_btn.clicked.connect(self.delete_conversation.emit)

        row.addWidget(self._new_btn, 1)
        row.addWidget(self._delete_btn, 0)

        self.setStyleSheet("""
            QPushButton#newConvBtn, QPushButton#deleteConvBtn {
                background: rgba(15, 15, 15, 200);
                color: #ffffff;
                border: 1px solid rgba(167, 139, 250, 0.30);
                border-radius: 18px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton#newConvBtn:hover, QPushButton#deleteConvBtn:hover {
                background: rgba(40, 28, 70, 220);
                border: 1px solid rgba(167, 139, 250, 0.65);
                color: #d4c5ff;
            }
            QPushButton#newConvBtn:pressed, QPushButton#deleteConvBtn:pressed {
                background: rgba(80, 50, 140, 230);
            }
            QPushButton#deleteConvBtn {
                /* slightly tighter so the trash button doesn't dominate */
                padding: 6px 12px;
            }
        """)

        self.setWindowOpacity(0.0)
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + 20,
        )
        # WakeScreen is also stays-on-top; raise above it to receive clicks.
        self.raise_()


# ── Settings panel ──────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    """Standalone dark-glassmorphism settings panel.

    Lists each API provider with an API-key field and the models it offers,
    plus a 'Smart model selection' toggle.
    """

    smart_routing_toggled = pyqtSignal(bool)
    api_keys_saved = pyqtSignal(dict)
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()

        self.setWindowTitle("OSIA Settings")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Must receive clicks — WakeScreen above is transparent for events,
        # but this widget is not.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.setFixedSize(540, 640)

        self._key_inputs: dict[str, QLineEdit] = {}
        self._build_ui()
        self._load_state()
        self._apply_style()

    # ── UI construction ────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)

        # Header row
        header_row = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("title")
        close_btn = QPushButton("✕")  # ✕
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self._on_close)
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(close_btn)
        outer.addLayout(header_row)

        # Smart routing row
        smart_row = QHBoxLayout()
        smart_label = QLabel("Smart model selection")
        smart_label.setObjectName("smartLabel")
        self._smart_toggle = QCheckBox()
        self._smart_toggle.setObjectName("smartToggle")
        self._smart_toggle.stateChanged.connect(self._on_smart_changed)
        smart_help = QLabel("Auto-pick a model based on the task.")
        smart_help.setObjectName("smartHelp")
        smart_row.addWidget(smart_label)
        smart_row.addStretch()
        smart_row.addWidget(self._smart_toggle)
        outer.addLayout(smart_row)
        outer.addWidget(smart_help)

        # Divider
        div = QFrame()
        div.setObjectName("divider")
        div.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(div)

        # Scroll area for providers
        scroll = QScrollArea()
        scroll.setObjectName("scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        container.setObjectName("scrollContainer")
        scroll_layout = QVBoxLayout(container)
        scroll_layout.setContentsMargins(0, 0, 8, 0)
        scroll_layout.setSpacing(14)

        # Startup (systemd services) section
        scroll_layout.addWidget(self._build_startup_section())

        # Voice section (TTS provider + voice)
        scroll_layout.addWidget(self._build_voice_section())

        for provider in API_PROVIDERS:
            scroll_layout.addWidget(self._build_provider_section(provider))

        # Ollama section (no key)
        scroll_layout.addWidget(self._build_ollama_section())

        scroll_layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        # Save button
        save_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.setObjectName("saveBtn")
        save_btn.setMinimumHeight(34)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._on_save)
        save_row.addStretch()
        save_row.addWidget(save_btn)
        outer.addLayout(save_row)

    def _build_provider_section(self, provider: str) -> QWidget:
        cfg = PROVIDERS[provider]
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        header = QLabel(cfg["name"])
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        models = [m for m in HARDCODED_MODELS if m["provider"] == provider]
        if models:
            names = ", ".join(m["label"] for m in models)
            sub = QLabel(names)
            sub.setObjectName("sectionModels")
            sub.setWordWrap(True)
            layout.addWidget(sub)

        key_row = QHBoxLayout()
        key_label = QLabel("API key")
        key_label.setObjectName("fieldLabel")
        key_label.setFixedWidth(60)
        key_input = QLineEdit()
        key_input.setObjectName("keyInput")
        key_input.setEchoMode(QLineEdit.EchoMode.Password)
        key_input.setPlaceholderText(f"{cfg['name']} API key")
        key_row.addWidget(key_label)
        key_row.addWidget(key_input)
        layout.addLayout(key_row)

        self._key_inputs[provider] = key_input
        return wrap

    # Provider key in settings -> dropdown label
    _TTS_PROVIDER_LABELS = [
        ("local",  "Local (Kokoro)"),
        ("openai", "OpenAI"),
        ("google", "Google Cloud"),
    ]

    def _build_voice_section(self) -> QWidget:
        from osia.voice.tts import (
            OPENAI_VOICES, GOOGLE_FR_VOICES, GOOGLE_EN_VOICES,
            PROVIDER_DEFAULT_VOICE,
        )
        self._openai_voices = list(OPENAI_VOICES)
        self._google_voices = [("French — " + v, v) for v in GOOGLE_FR_VOICES] + \
                              [("English — " + v, v) for v in GOOGLE_EN_VOICES]
        self._provider_default_voice = dict(PROVIDER_DEFAULT_VOICE)

        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QLabel("Voice")
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        sub = QLabel("Choose how OSIA speaks. Changes apply immediately.")
        sub.setObjectName("sectionModels")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # Provider row
        prov_row = QHBoxLayout()
        prov_label = QLabel("Provider")
        prov_label.setObjectName("fieldLabel")
        prov_label.setFixedWidth(60)
        self._tts_provider_combo = QComboBox()
        self._tts_provider_combo.setObjectName("voiceCombo")
        for key, label in self._TTS_PROVIDER_LABELS:
            self._tts_provider_combo.addItem(label, key)
        self._tts_provider_combo.currentIndexChanged.connect(self._on_tts_provider_changed)
        prov_row.addWidget(prov_label)
        prov_row.addWidget(self._tts_provider_combo, 1)
        layout.addLayout(prov_row)

        # Voice row (hidden when provider is "local")
        self._tts_voice_row_widget = QWidget()
        voice_row = QHBoxLayout(self._tts_voice_row_widget)
        voice_row.setContentsMargins(0, 0, 0, 0)
        voice_label = QLabel("Voice")
        voice_label.setObjectName("fieldLabel")
        voice_label.setFixedWidth(60)
        self._tts_voice_combo = QComboBox()
        self._tts_voice_combo.setObjectName("voiceCombo")
        self._tts_voice_combo.currentIndexChanged.connect(self._on_tts_voice_changed)
        voice_row.addWidget(voice_label)
        voice_row.addWidget(self._tts_voice_combo, 1)
        layout.addWidget(self._tts_voice_row_widget)

        # API key row (shown only for cloud providers — openai / google)
        self._voice_api_key_row_widget = QWidget()
        voice_key_row = QHBoxLayout(self._voice_api_key_row_widget)
        voice_key_row.setContentsMargins(0, 0, 0, 0)
        voice_key_label = QLabel("API key")
        voice_key_label.setObjectName("fieldLabel")
        voice_key_label.setFixedWidth(60)
        self._voice_api_key_input = QLineEdit()
        self._voice_api_key_input.setObjectName("keyInput")
        self._voice_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._voice_api_key_input.textChanged.connect(self._on_voice_api_key_changed)
        voice_key_row.addWidget(voice_key_label)
        voice_key_row.addWidget(self._voice_api_key_input, 1)
        layout.addWidget(self._voice_api_key_row_widget)

        return wrap

    def _populate_voice_combo(self, provider: str, selected_voice: str | None):
        """Refill the voice dropdown for the given provider and try to
        restore the saved selection."""
        combo = self._tts_voice_combo
        combo.blockSignals(True)
        combo.clear()
        if provider == "openai":
            for v in self._openai_voices:
                combo.addItem(v, v)
        elif provider == "google":
            for label, v in self._google_voices:
                combo.addItem(label, v)
        # provider == "local" -> no items; row will be hidden
        if selected_voice:
            for i in range(combo.count()):
                if combo.itemData(i) == selected_voice:
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)
        self._tts_voice_row_widget.setVisible(provider != "local")
        self._voice_api_key_row_widget.setVisible(provider in ("openai", "google"))

    def _populate_voice_api_key(self, provider: str):
        """Load the API key for the current cloud provider into the voice
        section's key input. No-op for local provider."""
        if provider not in ("openai", "google"):
            return
        provider_label = PROVIDERS[provider]["name"]
        self._voice_api_key_input.setPlaceholderText(f"{provider_label} API key")
        keys = load_api_keys()
        self._voice_api_key_input.blockSignals(True)
        self._voice_api_key_input.setText(keys.get(provider, ""))
        self._voice_api_key_input.blockSignals(False)

    def _on_tts_provider_changed(self, _idx: int):
        provider = self._tts_provider_combo.currentData()
        set_setting("tts_provider", provider)
        # When switching providers, snap voice to that provider's default so
        # we never persist an OpenAI voice while Google is active, etc.
        default_voice = self._provider_default_voice.get(provider)
        set_setting("tts_voice", default_voice)
        self._populate_voice_combo(provider, default_voice)
        self._populate_voice_api_key(provider)

    def _on_tts_voice_changed(self, _idx: int):
        voice = self._tts_voice_combo.currentData()
        if voice:
            set_setting("tts_voice", voice)

    def _on_voice_api_key_changed(self, text: str):
        provider = self._tts_provider_combo.currentData()
        if provider not in ("openai", "google"):
            return
        set_api_key(provider, text.strip())
        # Keep the AI Models section's input in sync so a later "Save" click
        # there doesn't overwrite what we just wrote.
        sibling = self._key_inputs.get(provider)
        if sibling is not None and sibling.text() != text:
            sibling.blockSignals(True)
            sibling.setText(text)
            sibling.blockSignals(False)

    def _build_startup_section(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QLabel("Démarrage")
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        sub = QLabel(
            "Lancer le serveur mobile et le tunnel Cloudflare au démarrage "
            "du système."
        )
        sub.setObjectName("sectionModels")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # One toggle row per service. Each row owns: label, status dot, toggle.
        # Each entry: (unit_name, display_label, dot_attr, toggle_attr)
        self._startup_refresh_guard = False
        self._startup_rows: list[tuple[str, QLabel, QCheckBox]] = []

        rows = [
            (STARTUP_SERVICES[0], "Serveur mobile"),
            (STARTUP_SERVICES[1], "Tunnel Cloudflare"),
        ]
        for unit, display in rows:
            row = QHBoxLayout()
            row.setSpacing(8)

            label = QLabel(display)
            label.setObjectName("startupLabel")
            dot = self._make_status_dot()
            toggle = QCheckBox()
            toggle.setObjectName("smartToggle")
            toggle.stateChanged.connect(
                lambda _s, u=unit, t=toggle: self._on_startup_toggle_changed(u, t)
            )

            row.addWidget(label)
            row.addStretch()
            row.addWidget(dot)
            row.addWidget(toggle)
            layout.addLayout(row)

            self._startup_rows.append((unit, dot, toggle))

        # Refresh timer — only ticks while the panel is visible.
        self._startup_timer = QTimer(self)
        self._startup_timer.setInterval(5000)
        self._startup_timer.timeout.connect(self._refresh_startup_status)

        return wrap

    def _make_status_dot(self) -> QLabel:
        dot = QLabel()
        dot.setObjectName("statusDot")
        dot.setFixedSize(10, 10)
        dot.setProperty("state", "stopped")
        return dot

    def _set_dot_state(self, dot: QLabel, state: str) -> None:
        """state ∈ {'running','stopped','error'}."""
        if dot.property("state") == state:
            return
        dot.setProperty("state", state)
        # Force a style re-evaluation since we drive color via the property.
        dot.style().unpolish(dot)
        dot.style().polish(dot)

    def _refresh_startup_status(self):
        # Toggle reflects whether the unit is enabled at boot; dot reflects
        # its current runtime state.
        self._startup_refresh_guard = True
        for unit, dot, toggle in self._startup_rows:
            self._set_dot_state(dot, get_service_status(unit))
            enabled = is_service_enabled(unit)
            if toggle.isChecked() != enabled:
                toggle.setChecked(enabled)
        self._startup_refresh_guard = False

    def _on_startup_toggle_changed(self, unit: str, toggle: QCheckBox):
        if self._startup_refresh_guard:
            return
        checked = toggle.isChecked()
        op = enable_service if checked else disable_service
        # The panel is FramelessWindowHint | WindowStaysOnTopHint, which
        # covers the pkexec polkit dialog. Hide ourselves while pkexec runs
        # so the auth prompt is reachable, then come back.
        self.hide()
        QApplication.processEvents()
        try:
            ok = op(unit)
        finally:
            self.show()
        if not ok:
            # pkexec was cancelled or systemctl failed — snap the toggle
            # back to whatever the real state is.
            self._startup_refresh_guard = True
            toggle.setChecked(not checked)
            self._startup_refresh_guard = False
        self._refresh_startup_status()

    def _build_ollama_section(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("section")
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        header = QLabel("Local (Ollama)")
        header.setObjectName("sectionTitle")
        layout.addWidget(header)

        sub = QLabel("Auto-detected via `ollama list`. No API key needed.")
        sub.setObjectName("sectionModels")
        sub.setWordWrap(True)
        layout.addWidget(sub)
        return wrap

    def _apply_style(self):
        # Soft drop shadow behind the panel
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 200))
        self.setGraphicsEffect(shadow)

        self.setStyleSheet("""
            QWidget {
                color: #ffffff;
                font-family: sans-serif;
            }
            QLabel#title {
                font-size: 16px;
                font-weight: 600;
                letter-spacing: 2px;
                text-transform: uppercase;
                color: rgba(255, 255, 255, 0.85);
            }
            QPushButton#closeBtn {
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 14px;
                color: rgba(255, 255, 255, 0.7);
                font-size: 14px;
            }
            QPushButton#closeBtn:hover {
                background: rgba(255, 255, 255, 0.12);
                color: #ffffff;
            }
            QLabel#smartLabel {
                font-size: 13px;
                font-weight: 500;
            }
            QLabel#smartHelp {
                font-size: 11px;
                color: rgba(255, 255, 255, 0.45);
                padding-left: 2px;
            }
            QFrame#divider {
                color: rgba(255, 255, 255, 0.08);
                background: rgba(255, 255, 255, 0.08);
                max-height: 1px;
                min-height: 1px;
                border: none;
            }
            QScrollArea#scroll, QWidget#scrollContainer {
                background: transparent;
                border: none;
            }
            QFrame#section {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 10px;
            }
            QLabel#sectionTitle {
                font-size: 13px;
                font-weight: 600;
                color: #ffffff;
            }
            QLabel#sectionModels {
                font-size: 11px;
                color: rgba(255, 255, 255, 0.55);
            }
            QLabel#fieldLabel {
                font-size: 11px;
                color: rgba(255, 255, 255, 0.6);
            }
            QLineEdit#keyInput {
                background: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 6px 10px;
                color: #ffffff;
                font-size: 12px;
            }
            QLineEdit#keyInput:focus {
                border: 1px solid rgba(255, 255, 255, 0.25);
            }
            QComboBox#voiceCombo {
                background: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 5px 10px;
                color: #ffffff;
                font-size: 12px;
            }
            QComboBox#voiceCombo:focus {
                border: 1px solid rgba(255, 255, 255, 0.25);
            }
            QComboBox#voiceCombo::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox#voiceCombo::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid rgba(255, 255, 255, 0.5);
                margin-right: 6px;
            }
            QComboBox#voiceCombo QAbstractItemView {
                background: rgba(20, 20, 20, 240);
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                selection-background-color: rgba(255, 255, 255, 0.15);
                font-size: 12px;
            }
            QPushButton#saveBtn {
                background: rgba(255, 255, 255, 0.1);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                padding: 6px 22px;
                color: #ffffff;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#saveBtn:hover {
                background: rgba(255, 255, 255, 0.18);
            }
            QPushButton#saveBtn:pressed {
                background: rgba(255, 255, 255, 0.25);
            }
            QLabel#startupLabel {
                font-size: 12px;
                color: rgba(255, 255, 255, 0.85);
            }
            QLabel#statusText {
                font-size: 11px;
                color: rgba(255, 255, 255, 0.6);
            }
            QLabel#statusDot {
                border-radius: 5px;
                background: rgba(180, 180, 180, 0.4);
            }
            QLabel#statusDot[state="running"] {
                background: rgb(100, 200, 120);
            }
            QLabel#statusDot[state="stopped"] {
                background: rgb(220, 80, 80);
            }
            QLabel#statusDot[state="error"] {
                background: rgb(220, 160, 60);
            }
            QCheckBox#smartToggle::indicator {
                width: 36px;
                height: 20px;
                border-radius: 10px;
                background: rgba(255, 255, 255, 0.15);
                border: 1px solid rgba(255, 255, 255, 0.15);
            }
            QCheckBox#smartToggle::indicator:checked {
                background: rgba(100, 200, 120, 0.85);
                border: 1px solid rgba(100, 200, 120, 0.95);
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

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(15, 15, 15, 230))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)

    # ── State ────────────────────────────────────────────────────────────

    def _load_state(self):
        keys = load_api_keys()
        for provider, edit in self._key_inputs.items():
            edit.setText(keys.get(provider, ""))
        settings = load_settings()
        self._smart_toggle.setChecked(bool(settings.get("smart_routing", False)))

        # Voice section
        provider = settings.get("tts_provider", "local") or "local"
        voice = settings.get("tts_voice")
        self._tts_provider_combo.blockSignals(True)
        for i in range(self._tts_provider_combo.count()):
            if self._tts_provider_combo.itemData(i) == provider:
                self._tts_provider_combo.setCurrentIndex(i)
                break
        self._tts_provider_combo.blockSignals(False)
        self._populate_voice_combo(provider, voice)
        self._populate_voice_api_key(provider)

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + (geo.height() - self.height()) // 2,
        )
        # WakeScreen is also stays-on-top; raise above it to receive clicks.
        self.raise_()
        self.activateWindow()
        # Refresh state every time it opens (keys may have changed externally).
        self._load_state()
        self._refresh_startup_status()
        self._startup_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._startup_timer.stop()

    # ── Handlers ────────────────────────────────────────────────────────

    def _on_smart_changed(self, state):
        checked = self._smart_toggle.isChecked()
        set_setting("smart_routing", checked)
        self.smart_routing_toggled.emit(checked)

    def _on_save(self):
        keys = load_api_keys()
        for provider, edit in self._key_inputs.items():
            val = edit.text().strip()
            if val:
                keys[provider] = val
            else:
                keys.pop(provider, None)
        save_api_keys(keys)
        self.api_keys_saved.emit(keys)

    def _on_close(self):
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._on_close()
            return
        super().keyPressEvent(event)


# ── Self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv)

    ws = WakeScreen()
    ms = ModelSelector()
    sb = SettingsButton()
    sp = SettingsPanel()
    cc = ConversationControls()

    ws.fade_in_started.connect(ms.fade_in)
    ws.fade_in_started.connect(sb.fade_in)
    ws.fade_in_started.connect(cc.fade_in)
    ws.fade_out_started.connect(ms.fade_out)
    ws.fade_out_started.connect(sb.fade_out)
    ws.fade_out_started.connect(cc.fade_out)

    sb.clicked.connect(sp.show)
    cc.new_conversation.connect(lambda: print("new conversation"))
    cc.delete_conversation.connect(lambda: print("delete conversation"))

    ws.show_wake()
    QTimer.singleShot(3000, ws.hide_wake)
    QTimer.singleShot(6000, ws.show_wake)
    QTimer.singleShot(10000, app.quit)
    sys.exit(app.exec())
