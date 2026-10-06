"""Release-only legal gate for the desktop application."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from osia import i18n, legal
from osia.gui.legal_viewer import LegalDocumentDialog


class LegalAcceptanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(i18n.t("legal.gate_title"))
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        intro = QLabel(i18n.t("legal.gate_desc"))
        intro.setWordWrap(True)
        layout.addWidget(intro)

        terms = QPushButton(i18n.t("legal.open_terms"))
        terms.clicked.connect(
            lambda: LegalDocumentDialog(self, "terms").exec()
        )
        privacy = QPushButton(i18n.t("legal.open_privacy"))
        privacy.clicked.connect(
            lambda: LegalDocumentDialog(self, "privacy").exec()
        )
        layout.addWidget(terms)
        layout.addWidget(privacy)

        self.acceptance = QCheckBox(
            i18n.t("legal.accept_terms", version=legal.TERMS_VERSION)
        )
        self.acceptance.setChecked(False)
        layout.addWidget(self.acceptance)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setEnabled(False)
        self.acceptance.toggled.connect(self.ok_button.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def maybe_show_release_legal_gate(parent=None) -> bool:
    decision = legal.startup_decision(legal.load_acceptance())
    if decision.show_privacy_update:
        webbrowser.open(legal.PRIVACY_URL)
        legal.record_privacy_seen()
    if not decision.require_terms_acceptance:
        return True
    dialog = LegalAcceptanceDialog(parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    legal.record_terms_acceptance()
    return True
