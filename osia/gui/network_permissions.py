"""Let users revisit a local-network decision in the system settings."""
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout

from osia import i18n


def network_settings_command() -> list[str]:
    if sys.platform == "win32":
        return [str(Path(os.environ["SystemRoot"]) / "System32" / "control.exe"),
                "/name", "Microsoft.WindowsFirewall", "/page", "pageConfigureApps"]
    if sys.platform == "darwin":
        return ["/usr/bin/open",
                "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"]
    for name in ("firewall-config", "gufw"):
        executable = shutil.which(name)
        if executable:
            return [executable]
    return []


class NetworkPermissions(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("section")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("sectionTitle")
        self.description = QLabel()
        self.description.setObjectName("sectionDesc")
        self.description.setWordWrap(True)
        self.button = QPushButton()
        self.button.setObjectName("primaryBtn")
        self.button.clicked.connect(self._review)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        for widget in (self.title, self.description, self.button, self.error):
            layout.addWidget(widget)
        self.retranslate()

    def retranslate(self):
        platform = {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")
        self.title.setText(i18n.t("permissions.network"))
        self.description.setText(i18n.t("permissions." + platform))
        self.button.setText(i18n.t("permissions.review"))
        self.error.setText(i18n.t("permissions.unavailable"))

    def _review(self):
        command = network_settings_command()
        started = bool(command) and QProcess.startDetached(command[0], command[1:])[0]
        self.error.setVisible(not started)
