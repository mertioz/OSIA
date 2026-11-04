"""A short illustrated introduction to the desktop Settings."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QPainter, QPen
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from osia import i18n
from osia.gui.theme import get_theme_manager
from osia.onboarding import mark_onboarding_handled


_CAPTIONS = {
    "en": (
        "Open Settings\nSelect the gear at the bottom.",
        "Make OSIA yours\nChoose your language and theme in General.",
        "Choose a model\nOpen Models to select the one you want.",
        "Set the voice\nOpen Voice to choose how OSIA sounds.",
    ),
    "fr": (
        "Ouvre les paramètres\nClique sur la roue en bas de la fenêtre.",
        "Personnalise OSIA\nLangue et thème se règlent dans Général.",
        "Choisis un modèle\nSélectionne-le dans Modèles.",
        "Règle la voix\nOuvre Voix pour choisir celle d’OSIA.",
    ),
}


class _SettingsIllustration(QWidget):
    """Draw a simple, theme-aware symbol for the current setting."""

    def __init__(self):
        super().__init__()
        self.setObjectName("guideIllustration")
        self._step = 0

    def set_step(self, step: int) -> None:
        self._step = step
        self.update()

    def paintEvent(self, event) -> None:
        palette = get_theme_manager().palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.setPen(QPen(palette.color("primary", 45), 1))
        painter.setBrush(palette.color("surface"))
        painter.drawRoundedRect(QRectF(-108, -60, 216, 120), 20, 20)

        accent = palette.color("primary")
        pen = QPen(accent, 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._step == 0:
            for y, x in ((-23, -16), (0, 17), (23, -4)):
                painter.drawLine(QPointF(-44, y), QPointF(44, y))
                painter.setBrush(palette.color("surface"))
                painter.drawEllipse(QPointF(x, y), 6, 6)
        elif self._step == 1:
            font = QFont()
            font.setFamilies(["Segoe UI", "Helvetica Neue", "Noto Sans", "sans-serif"])
            font.setPixelSize(28)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(-71, -25, 70, 50), Qt.AlignmentFlag.AlignCenter, "Aa")
            painter.setPen(Qt.PenStyle.NoPen)
            swatches = ((6, accent), (29, palette.color("secondary")), (52, palette.color("text")))
            for x, shade in swatches:
                painter.setBrush(shade)
                painter.drawRoundedRect(QRectF(x, -11, 17, 22), 4, 4)
        elif self._step == 2:
            painter.drawRoundedRect(QRectF(-64, -21, 128, 42), 9, 9)
            painter.drawLine(QPointF(-45, 0), QPointF(16, 0))
            painter.drawLine(QPointF(36, -3), QPointF(43, 4))
            painter.drawLine(QPointF(43, 4), QPointF(50, -3))
        else:
            for index, height in enumerate((8, 17, 27, 38, 27, 17, 8)):
                x = (index - 3) * 14
                painter.drawLine(QPointF(x, -height / 2), QPointF(x, height / 2))
        painter.end()


class GuideSlideshowDialog(QDialog):
    """Show one image and two short lines per step."""

    handled = pyqtSignal(str)
    SLIDE_COUNT = len(_CAPTIONS["en"])

    def __init__(
        self, parent: QWidget | None = None, *,
        automatic: bool = False, state_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._automatic = automatic
        self._state_path = state_path
        self._handled = False
        self._index = 0
        self._theme = get_theme_manager()
        self.setObjectName("onboardingDialog")
        self.setMinimumSize(370, 240)
        self.resize(460, 300)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 18)
        outer.setSpacing(8)
        self._illustration = _SettingsIllustration()
        outer.addWidget(self._illustration, 1)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self._caption = QLabel()
        self._caption.setObjectName("guideCaption")
        self._caption.setTextFormat(Qt.TextFormat.PlainText)
        footer.addWidget(self._caption, 1)
        self._next = QPushButton("→")
        self._next.setObjectName("guideNext")
        self._next.setFixedSize(40, 40)
        self._next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self._next.clicked.connect(self._advance)
        footer.addWidget(self._next)
        outer.addLayout(footer)

        self._theme.theme_changed.connect(self._apply_style)
        i18n.add_listener(self._render)
        self.destroyed.connect(lambda: i18n.remove_listener(self._render))
        self._apply_style()
        self._render()

    def _render(self) -> None:
        caption = _CAPTIONS.get(i18n.get_language(), _CAPTIONS["en"])[self._index]
        self.setWindowTitle(i18n.t("guide.window_title"))
        self._caption.setText(caption)
        self._illustration.set_step(self._index)
        self._illustration.setAccessibleName(caption.splitlines()[0])
        label = i18n.t("guide.finish" if self._index == self.SLIDE_COUNT - 1 else "guide.next")
        self._next.setAccessibleName(label)
        self._next.setToolTip(label)

    def _advance(self) -> None:
        if self._index + 1 < self.SLIDE_COUNT:
            self._index += 1
            self._render()
        else:
            self.accept()

    def done(self, result: int) -> None:
        if not self._handled:
            self._handled = True
            outcome = "completed" if result == QDialog.DialogCode.Accepted else "closed"
            if self._automatic:
                try:
                    mark_onboarding_handled(outcome, self._state_path)
                except OSError:
                    pass
            self.handled.emit(outcome)
        super().done(result)

    def _apply_style(self, _key: str = "") -> None:
        palette = self._theme.palette
        self.setStyleSheet(f"""
            QDialog#onboardingDialog {{
                background: {palette.background};
            }}
            QLabel#guideCaption {{
                color: {palette.text};
                font-family: "Segoe UI", "Helvetica Neue", "Noto Sans", sans-serif;
                font-size: 14px;
            }}
            QPushButton#guideNext {{
                color: {palette.background};
                background: {palette.primary};
                border: 2px solid transparent;
                border-radius: 12px;
                font-family: "Segoe UI", "Helvetica Neue", "Noto Sans", sans-serif;
                font-size: 23px;
            }}
            QPushButton#guideNext:hover, QPushButton#guideNext:focus {{
                border-color: {palette.text};
            }}
        """)
        self._illustration.update()


__all__ = ["GuideSlideshowDialog"]
