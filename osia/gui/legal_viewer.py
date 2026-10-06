"""Offline Terms and Privacy reader."""

from __future__ import annotations

import webbrowser

from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from osia import i18n, legal


class LegalDocumentDialog(QDialog):
    def __init__(self, parent=None, document: str = "terms"):
        super().__init__(parent)
        self._document = document if document in {"terms", "privacy"} else "terms"
        active_language = i18n.get_language()
        self._locale = "fr" if active_language.startswith("fr") else "en"
        self.setMinimumSize(620, 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        self._title = QLabel()
        self._title.setStyleSheet("font-size: 19px; font-weight: 700;")
        header.addWidget(self._title)
        header.addStretch()
        self._language = QComboBox()
        self._language.addItem("English", "en")
        self._language.addItem("Français", "fr")
        self._language.setCurrentIndex(0 if self._locale == "en" else 1)
        self._language.currentIndexChanged.connect(self._on_language_changed)
        header.addWidget(self._language)
        layout.addLayout(header)

        self._metadata = QLabel()
        self._metadata.setWordWrap(True)
        self._metadata.setStyleSheet("color: #a39bad;")
        layout.addWidget(self._metadata)

        self._body = QTextBrowser()
        self._body.setReadOnly(True)
        self._body.setOpenExternalLinks(True)
        self._body.setStyleSheet(
            "QTextBrowser { background: transparent; border: 0; "
            "color: #ddd8e6; font-size: 14px; line-height: 1.5; }"
        )
        layout.addWidget(self._body, 1)

        buttons = QHBoxLayout()
        self._online = QPushButton()
        self._online.clicked.connect(self._open_online)
        buttons.addWidget(self._online)
        buttons.addStretch()
        close = QPushButton(i18n.t("settings.close"))
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._refresh()

    def _on_language_changed(self, _index: int) -> None:
        value = self._language.currentData()
        if value in {"en", "fr"}:
            self._locale = value
            self._refresh()

    def _refresh(self) -> None:
        document = legal.get_document(self._document, self._locale)
        title = str(document.get("title", ""))
        self.setWindowTitle(title)
        self._title.setText(title)
        updated = legal.LAST_UPDATED
        if self._locale == "fr":
            metadata = f"Version {legal.TERMS_VERSION} · mise à jour {updated} · contact : {legal.PUBLIC_CONTACT_EMAIL}"
            self._online.setText("Voir en ligne")
        else:
            metadata = f"Version {legal.TERMS_VERSION} · updated {updated} · contact: {legal.PUBLIC_CONTACT_EMAIL}"
            self._online.setText("View online")
        if self._document == "terms":
            metadata += (
                " · entrée en vigueur à la publication"
                if self._locale == "fr"
                else " · effective on publication"
            )
        self._metadata.setText(metadata)
        self._body.setMarkdown(str(document.get("markdown", "")))
        self._body.moveCursor(QTextCursor.MoveOperation.Start)

    def _open_online(self) -> None:
        url = legal.online_url(self._document, self._locale)
        if url:
            webbrowser.open(url)
