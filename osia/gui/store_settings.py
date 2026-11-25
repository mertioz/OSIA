"""Update and feedback actions in the Microsoft Store edition."""
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from osia import i18n
from osia.version import PRODUCT_VERSION
from osia.windows_store import PRODUCT_ID


class StoreSettingsSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("section")
        layout = QVBoxLayout(self)
        version = QLabel(f"OSIA {PRODUCT_VERSION}")
        version.setObjectName("sectionTitle")
        layout.addWidget(version)
        self.updates = QPushButton()
        self.report = QPushButton()
        for button in (self.updates, self.report):
            button.setObjectName("ghostBtn")
            layout.addWidget(button)
        self.updates.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl(f"ms-windows-store://pdp/?ProductId={PRODUCT_ID}")))
        self.report.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("mailto:contact@osia.space?subject=OSIA%20-%20AI%20content%20report")))
        i18n.add_listener(self._retranslate)
        self._retranslate()

    def _retranslate(self):
        self.updates.setText(i18n.t("store.updates"))
        self.report.setText(i18n.t("store.report"))
