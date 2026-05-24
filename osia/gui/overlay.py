#!/usr/bin/env python3
"""
OSIA GUI Overlay — floating always-on-top window using PyQt6.
Dark glassmorphism style, positioned right side of screen.
Contains: model selector, chat input, response area, status indicator.
"""

import subprocess
import threading
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QLineEdit, QPushButton, QTextEdit,
    QSizePolicy,
)
from PyQt6.QtCore import (
    Qt, QRect, pyqtSignal, QObject, QTimer, QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QColor, QPainter, QBrush, QPen, QFont, QFontMetrics,
)


def get_ollama_models():
    """Run 'ollama list' and return list of model names."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().split("\n")
        models = []
        for line in lines[1:]:  # Skip header
            parts = line.split()
            if parts:
                models.append(parts[0])
        return models if models else ["llama3"]
    except Exception:
        return ["llama3"]


class OSIAOverlay(QWidget):
    """Floating glassmorphism overlay for OSIA."""

    # Signals for inter-thread communication
    status_changed = pyqtSignal(str)  # 'idle', 'listening', 'thinking'
    response_ready = pyqtSignal(str)  # response text
    model_changed = pyqtSignal(str)   # new model name
    send_message = pyqtSignal(str)    # user chat message

    def __init__(self):
        super().__init__()

        # Drag state
        self._drag_pos = None

        # Window setup
        self.setWindowTitle("OSIA")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        # Size
        self.setFixedWidth(360)
        self.setMinimumHeight(260)

        self._setup_ui()
        self._apply_style()

        # Status
        self._current_status = "idle"

        # Position: right side, vertically at 55% — must be after show()
        self.show()
        screen = QApplication.primaryScreen().geometry()
        x = screen.width() - 380
        y = int(screen.height() * 0.55)
        self.move(x, y)

    def _setup_ui(self):
        """Build the UI layout."""
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(10)

        # Header
        header = QLabel("OSIA")
        header.setAlignment(Qt.AlignmentFlag.AlignLeft)
        header.setObjectName("header")
        layout.addWidget(header)

        # Model selector
        model_layout = QHBoxLayout()
        model_label = QLabel("Model:")
        model_label.setObjectName("modelLabel")
        model_label.setFixedWidth(45)
        self.model_combo = QComboBox()
        self.model_combo.setObjectName("modelCombo")
        self.model_combo.setMinimumHeight(28)
        # Populate models
        models = get_ollama_models()
        self.model_combo.addItems(models)
        self.model_combo.currentTextChanged.connect(self._on_model_change)
        model_layout.addWidget(model_label)
        model_layout.addWidget(self.model_combo)
        layout.addLayout(model_layout)

        # Chat input row
        chat_layout = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setPlaceholderText("Message...")
        self.chat_input.setMinimumHeight(32)
        self.chat_input.returnPressed.connect(self._on_send)
        send_btn = QPushButton("Send")
        send_btn.setObjectName("sendBtn")
        send_btn.setMinimumHeight(32)
        send_btn.clicked.connect(self._on_send)
        chat_layout.addWidget(self.chat_input, 1)
        chat_layout.addWidget(send_btn)
        layout.addLayout(chat_layout)

        # Response area
        self.response_area = QTextEdit()
        self.response_area.setObjectName("responseArea")
        self.response_area.setReadOnly(True)
        self.response_area.setMaximumHeight(80)
        self.response_area.setMinimumHeight(40)
        self.response_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.response_area)

        # Status indicator row
        status_layout = QHBoxLayout()
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(10, 10)
        self.status_label = QLabel("Idle")
        self.status_label.setObjectName("statusLabel")
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        self.setLayout(layout)

    def _apply_style(self):
        """Apply dark glassmorphism stylesheet."""
        self.setStyleSheet("""
            QWidget#OSIAOverlay {
                background: rgba(15, 15, 15, 200);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 16px;
                font-family: sans-serif;
                color: #ffffff;
            }
            QLabel#header {
                font-size: 13px;
                font-weight: 600;
                color: rgba(255, 255, 255, 0.45);
                letter-spacing: 2px;
                text-transform: uppercase;
            }
            QLabel#modelLabel {
                font-size: 12px;
                color: rgba(255, 255, 255, 0.6);
            }
            QComboBox#modelCombo {
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                padding: 4px 8px;
                color: #ffffff;
                font-size: 12px;
            }
            QComboBox#modelCombo::drop-down {
                border: none;
            }
            QComboBox#modelCombo QAbstractItemView {
                background: rgba(25, 25, 25, 230);
                border: 1px solid rgba(255, 255, 255, 0.1);
                color: #ffffff;
                selection-background-color: rgba(255, 255, 255, 0.12);
            }
            QLineEdit#chatInput {
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 10px;
                padding: 6px 12px;
                color: #ffffff;
                font-size: 13px;
            }
            QLineEdit#chatInput:focus {
                border: 1px solid rgba(255, 255, 255, 0.25);
            }
            QPushButton#sendBtn {
                background: rgba(255, 255, 255, 0.1);
                border: none;
                border-radius: 10px;
                padding: 6px 16px;
                color: #ffffff;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton#sendBtn:hover {
                background: rgba(255, 255, 255, 0.18);
            }
            QPushButton#sendBtn:pressed {
                background: rgba(255, 255, 255, 0.25);
            }
            QTextEdit#responseArea {
                background: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 10px;
                padding: 8px 10px;
                color: #ffffff;
                font-size: 13px;
            }
            QLabel#statusLabel {
                font-size: 11px;
                color: rgba(255, 255, 255, 0.5);
            }
        """)

    # ── Drag support (frameless window) ──────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    # ── Public API ───────────────────────────────────────────────────

    def set_status(self, status: str):
        """Set status: 'idle', 'listening', 'thinking'."""
        self._current_status = status
        colors = {
            "idle": "rgba(120, 120, 120, 0.8)",
            "listening": "rgba(0, 220, 100, 0.9)",
            "thinking": "rgba(255, 170, 50, 0.9)",
        }
        labels = {
            "idle": "Idle",
            "listening": "Listening...",
            "thinking": "Thinking...",
        }
        color = colors.get(status, colors["idle"])
        text = labels.get(status, labels["idle"])
        self.status_dot.setStyleSheet(
            f"background: {color}; border-radius: 5px; min-width: 10px; min-height: 10px;"
        )
        self.status_label.setText(text)

    def set_response(self, text: str):
        """Show response in the response area."""
        self.response_area.setPlainText(text)

    def _on_model_change(self, model: str):
        """Emit signal when user changes model."""
        if model:
            self.model_changed.emit(model)

    def _on_send(self):
        """Emit signal when user sends a chat message."""
        text = self.chat_input.text().strip()
        if text:
            self.send_message.emit(text)
            self.chat_input.clear()

    def paintEvent(self, event):
        """Custom paint for glassmorphism background with rounded corners."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(15, 15, 15, 200))
        painter.setPen(QPen(QColor(255, 255, 255, 20), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    overlay = OSIAOverlay()
    # show() already called inside __init__ for positioning
    sys.exit(app.exec())
