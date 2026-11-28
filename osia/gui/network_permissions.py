"""Let users revisit a local-network decision in the system settings."""
import os
import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QFrame, QLabel, QMessageBox, QPushButton, QVBoxLayout

from osia import i18n, windows_store


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


def request_mobile_permission(parent=None, *, review=False) -> bool:
    """Only the foreground desktop asks. Background processes read the decision."""
    if not windows_store.is_store_install():
        return True
    key = windows_store.MOBILE_NETWORK_PERMISSION
    previous = windows_store.permission_decision(key)
    if previous is not None and not review:
        return previous
    allowed = windows_store.equivalent_mobile_firewall_choice() if previous is None and not review else None
    if allowed is None:
        answer = QMessageBox.question(parent, i18n.t("permissions.network"),
                                      i18n.t("permissions.mobile_question"),
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        allowed = answer == QMessageBox.StandardButton.Yes
    try:
        windows_store.remember_permission(key, allowed)
    except OSError:
        QMessageBox.warning(parent, i18n.t("permissions.network"), i18n.t("permissions.save_failed"))
        return False
    return allowed


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
        self.mobile = QPushButton()
        self.mobile.setObjectName("ghostBtn")
        self.mobile.setVisible(windows_store.is_store_install())
        self.mobile.clicked.connect(self._review_mobile)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.hide()
        for widget in (self.title, self.description, self.mobile, self.button, self.error):
            layout.addWidget(widget)
        self.retranslate()

    def retranslate(self):
        platform = {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")
        self.title.setText(i18n.t("permissions.network"))
        self.description.setText(i18n.t("permissions." + platform))
        self.mobile.setText(i18n.t("permissions.mobile_review"))
        self.button.setText(i18n.t("permissions.review"))
        self.error.setText(i18n.t("permissions.unavailable"))

    def _review_mobile(self):
        from osia.utils.systemd_manager import is_service_enabled, restart_service, stop_service
        previous = windows_store.permission_decision(windows_store.MOBILE_NETWORK_PERMISSION)
        current = request_mobile_permission(self, review=True)
        if current != previous:
            # Restart only OSIA's mobile server so new grants and revocations apply.
            def apply():
                if is_service_enabled("osia-server.service"):
                    restart_service("osia-server.service")
                elif not current:
                    stop_service("osia-server.service")
            threading.Thread(target=apply, name="osia-mobile-permission", daemon=True).start()

    def _review(self):
        command = network_settings_command()
        started = bool(command) and QProcess.startDetached(command[0], command[1:])[0]
        self.error.setVisible(not started)
