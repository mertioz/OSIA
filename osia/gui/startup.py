"""Early visual feedback for interactive desktop launches."""

import sys

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QWidget

from osia import i18n
from osia.utils.assets import ICON_PATH
from osia.version import current_product_version


def show_startup():
    app = QApplication.instance()
    if app is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(sys.argv)
    app.setDesktopFileName("osia")
    app.setApplicationName("OSIA")
    app.setApplicationDisplayName("OSIA")
    app.setApplicationVersion(current_product_version())
    icon = QIcon(ICON_PATH)
    app.setWindowIcon(icon)

    window = QWidget(None, Qt.WindowType.Window | Qt.WindowType.CustomizeWindowHint
                     | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowDoesNotAcceptFocus)
    window.setWindowTitle("OSIA")
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
    layout = QHBoxLayout(window)
    layout.setContentsMargins(20, 14, 24, 14)
    logo = QLabel()
    logo.setPixmap(icon.pixmap(32, 32))
    layout.addWidget(logo)
    layout.addWidget(QLabel(i18n.t("startup.loading")))
    window.setFixedSize(window.sizeHint())
    window.show()
    app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
    return app, window
