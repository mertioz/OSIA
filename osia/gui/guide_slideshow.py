"""A short illustrated introduction to the desktop Settings."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QFont, QGuiApplication, QKeySequence, QPainter, QRadialGradient
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from osia import i18n
from osia.gui.theme import get_theme_manager
from osia.onboarding import mark_onboarding_handled


_SLIDES = {
    "en": (
        ("Open Settings", "Click the capsule to open OSIA. Select the gear in the bottom bar."),
        ("Set the interface language", "In General, open Language and choose the language you want. The interface updates right away."),
        ("Pick a theme", "Open Themes in General. Select a color to preview it on the interface."),
        ("Choose a time format", "In General, choose a 12-hour or 24-hour clock. Conversation timestamps use that format."),
        ("Connect ChatGPT", "In Settings, open Models and pick ChatGPT from the provider menu. Click Connect and finish signing in in your browser."),
        ("Add an API provider", "In Settings → Models, choose an API provider such as DeepSeek. Enter your key in its card and save it."),
        ("Open the model picker", "Click the sparkle in the bottom bar. This menu selects the model that will answer your messages."),
        ("Open a provider’s list", "Select a provider card to see its models. If your provider is missing, check its connection in Settings → Models."),
        ("Select the model", "Click Use beside the model you want. The Active label confirms your selection for new messages."),
        ("Set Thinking when available", "Use the small gear beside a model to see its Thinking options. Start with Auto when it is offered."),
        ("Choose a voice provider", "In Settings → Voice, choose the provider that speaks OSIA’s replies. The local Kokoro option works with no API key."),
        ("Set up speech recognition", "In Voice, find the speech-to-text section and choose a provider. Press Ctrl+Alt+P to try manual listening."),
        ("Show or hide the capsule", "The capsule visibility switch is in General. Ctrl+Alt+O opens the OSIA window when the capsule is hidden."),
        ("Check for updates", "Open Updates in Settings to see the installed version. Use the check button to look for a newer version."),
    ),
    "fr": (
        ("Ouvre les paramètres", "Clique sur la capsule pour ouvrir OSIA. Sélectionne la roue dentée dans la barre du bas."),
        ("Choisis la langue de l’interface", "Dans Général, ouvre Langue et sélectionne celle que tu veux. L’interface se met à jour immédiatement."),
        ("Choisis un thème", "Ouvre Thèmes dans Général. Sélectionne une couleur pour voir le résultat sur l’interface."),
        ("Règle l’affichage de l’heure", "Dans Général, choisis le format 12 h ou 24 h. Les heures des discussions suivent ce format."),
        ("Connecte ChatGPT", "Dans Paramètres → Modèles, choisis ChatGPT. Clique sur Connecter et termine la connexion dans le navigateur."),
        ("Ajoute un fournisseur API", "Dans Paramètres → Modèles, choisis un fournisseur API comme DeepSeek. Saisis ta clé dans sa fiche et enregistre-la."),
        ("Ouvre le sélecteur de modèles", "Clique sur l’étoile dans la barre du bas. Ce menu sert à choisir le modèle qui répondra à tes messages."),
        ("Affiche les modèles d’un fournisseur", "Clique sur la carte d’un fournisseur pour voir sa liste. S’il manque, vérifie sa connexion dans Paramètres → Modèles."),
        ("Sélectionne le modèle", "Clique sur Utiliser à côté du modèle voulu. Le badge Actif confirme ton choix pour les nouveaux messages."),
        ("Règle Thinking si disponible", "La petite roue à côté d’un modèle ouvre ses options Thinking. Commence par Auto si l’option est proposée."),
        ("Choisis le fournisseur de voix", "Dans Paramètres → Voix, choisis le fournisseur qui lit les réponses d’OSIA. L’option locale Kokoro fonctionne sans clé API."),
        ("Règle la reconnaissance vocale", "Dans Voix → Reconnaissance vocale, choisis un fournisseur. Appuie sur Ctrl+Alt+P pour tester l’écoute."),
        ("Affiche ou masque la capsule", "L’interrupteur de la capsule se trouve dans Général. Ctrl+Alt+O ouvre la fenêtre OSIA lorsque la capsule est masquée."),
        ("Vérifie les mises à jour", "Dans Paramètres → Mises à jour, vois la version installée. Lance la recherche pour savoir si une nouvelle version est disponible."),
    ),
}

_ILLUSTRATION_MARKS = (
    "⚙︎", "Aa", "◈", "◷", "↗", "•••", "✦", "⊞", "✓", "⋯", "♪", "∿", "▬", "↓",
)


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
        scale = min(self.width() / 400, self.height() / 195, 1.35)
        painter.scale(scale, scale)

        painter.save()
        painter.scale(2.0, 1.0)
        glow = QRadialGradient(QPointF(0, 0), 96)
        glow.setColorAt(0.0, palette.color("primary", 42))
        glow.setColorAt(1.0, palette.color("primary", 0))
        painter.fillRect(QRectF(-100, -97, 200, 194), QBrush(glow))
        painter.restore()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color("surface"))
        painter.drawRoundedRect(QRectF(-164, -74, 328, 148), 24, 24)

        tile = QRectF(-139, -48, 96, 96)
        painter.setBrush(palette.color("primary", 28))
        painter.drawRoundedRect(tile, 20, 20)
        font = QFont()
        font.setFamilies(["Segoe UI Symbol", "Segoe UI", "Helvetica Neue", "Noto Sans", "sans-serif"])
        font.setPixelSize(37)
        painter.setFont(font)
        painter.setPen(palette.color("primary"))
        painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, _ILLUSTRATION_MARKS[self._step])

        painter.setPen(Qt.PenStyle.NoPen)
        for row, width in enumerate((130, 104, 77)):
            y = (row - 1) * 25
            color = palette.color("primary", 155) if row == self._step % 3 else palette.color("text", 32)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(-15, y - 3, width, 6), 3, 3)
        painter.end()


class GuideSlideshowDialog(QDialog):
    """Show one illustration and a short explanation per step."""

    handled = pyqtSignal(str)
    SLIDE_COUNT = len(_SLIDES["en"])

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
        self.setMinimumSize(440, 330)
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        self.resize(
            min(720, area.width() - 32) if area is not None else 720,
            min(480, area.height() - 44) if area is not None else 480,
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 28, 26)
        outer.setSpacing(16)
        self._illustration = _SettingsIllustration()
        outer.addWidget(self._illustration, 1)

        footer = QHBoxLayout()
        footer.setSpacing(20)
        copy = QVBoxLayout()
        copy.setSpacing(5)
        self._step_label = QLabel()
        self._step_label.setObjectName("guideStep")
        copy.addWidget(self._step_label)
        self._title = QLabel()
        self._title.setObjectName("guideTitle")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setWordWrap(True)
        copy.addWidget(self._title)
        self._caption = QLabel()
        self._caption.setObjectName("guideCaption")
        self._caption.setTextFormat(Qt.TextFormat.PlainText)
        self._caption.setWordWrap(True)
        copy.addWidget(self._caption)
        footer.addLayout(copy, 1)
        self._next = QPushButton("→")
        self._next.setObjectName("guideNext")
        self._next.setFlat(True)
        self._next.setFixedSize(58, 56)
        self._next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next.setShortcut(QKeySequence(Qt.Key.Key_Right))
        self._next.clicked.connect(self._advance)
        footer.addWidget(self._next, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(footer)

        self._theme.theme_changed.connect(self._apply_style)
        i18n.add_listener(self._render)
        self.destroyed.connect(lambda: i18n.remove_listener(self._render))
        self._apply_style()
        self._render()

    def _render(self) -> None:
        title, caption = _SLIDES.get(i18n.get_language(), _SLIDES["en"])[self._index]
        self.setWindowTitle(i18n.t("guide.window_title"))
        self._step_label.setText(f"{self._index + 1:02d} / {self.SLIDE_COUNT:02d}")
        self._title.setText(title)
        self._caption.setText(caption)
        self._illustration.set_step(self._index)
        self._illustration.setAccessibleName(title)
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
                font-family: "Segoe UI", "Helvetica Neue", "Noto Sans", sans-serif;
            }}
            QLabel#guideStep {{
                color: {palette.primary};
                font-size: 11px;
                font-weight: 600;
            }}
            QLabel#guideTitle {{
                color: {palette.text};
                font-size: 22px;
                font-weight: 600;
            }}
            QLabel#guideCaption {{
                color: {palette.muted_text};
                font-size: 14px;
            }}
            QPushButton#guideNext {{
                color: {palette.primary};
                background: transparent;
                border: none;
                padding: 0;
                font-family: "Segoe UI", "Helvetica Neue", "Noto Sans", sans-serif;
                font-size: 39px;
            }}
            QPushButton#guideNext:hover, QPushButton#guideNext:focus {{
                color: {palette.text};
            }}
        """)
        self._illustration.update()


__all__ = ["GuideSlideshowDialog"]
