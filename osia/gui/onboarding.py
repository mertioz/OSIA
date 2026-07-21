"""Short native Qt onboarding journey for a genuine first installation."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from osia import i18n
from osia.onboarding import mark_onboarding_handled
from osia.utils.assets import LOGO_PATH


class OnboardingDialog(QDialog):
    """Four concise pages; configuration remains entirely optional."""

    answer_settings_requested = pyqtSignal(bool)
    phone_settings_requested = pyqtSignal()
    handled = pyqtSignal(str)

    PAGE_COUNT = 4

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        automatic: bool = False,
        state_path=None,
    ) -> None:
        super().__init__(parent)
        self._automatic = bool(automatic)
        self._state_path = state_path
        self._handled = False
        self.setObjectName("onboardingDialog")
        self.setWindowTitle("OSIA")
        self.setModal(False)
        self.resize(520, 430)
        self.setMinimumSize(440, 380)
        self.setMaximumSize(620, 500)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)

        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(LOGO_PATH)
        if not pixmap.isNull():
            dpr = logo.devicePixelRatioF() or 1.0
            scaled = pixmap.scaledToHeight(
                max(1, round(58 * dpr)),
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            logo.setPixmap(scaled)
        outer.addWidget(logo)

        self._stack = QStackedWidget()
        self._stack.setObjectName("onboardingStack")
        self._pages = [
            self._page("onboarding.welcome_title", "onboarding.welcome_body"),
            self._answers_page(),
            self._phone_page(),
            self._page("onboarding.ready_title", "onboarding.ready_body"),
        ]
        for page in self._pages:
            self._stack.addWidget(page)
        outer.addWidget(self._stack, 1)

        self._progress = QLabel()
        self._progress.setObjectName("sectionSub")
        self._progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._progress)

        navigation = QHBoxLayout()
        navigation.setSpacing(8)
        self._skip = QPushButton()
        self._skip.setObjectName("linkBtn")
        self._back = QPushButton()
        self._back.setObjectName("ghostBtn")
        self._next = QPushButton()
        self._next.setObjectName("primaryBtn")
        self._skip.clicked.connect(lambda: self._finish("skipped"))
        self._back.clicked.connect(self._go_back)
        self._next.clicked.connect(self._go_next)
        navigation.addWidget(self._skip)
        navigation.addStretch(1)
        navigation.addWidget(self._back)
        navigation.addWidget(self._next)
        outer.addLayout(navigation)

        self._stack.currentChanged.connect(self._sync_navigation)
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))
        self._retranslate()
        self._sync_navigation(0)

    @staticmethod
    def _plain_label(object_name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        return label

    def _page(self, title_key: str, body_key: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        layout.addStretch(1)
        title = self._plain_label("panelTitle")
        body = self._plain_label("sectionDesc")
        title.setProperty("i18nKey", title_key)
        body.setProperty("i18nKey", body_key)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addStretch(1)
        return page

    def _answers_page(self) -> QWidget:
        page = self._page("onboarding.answers_title", "onboarding.answers_body")
        layout = page.layout()
        layout.takeAt(layout.count() - 1)
        self._chatgpt = QPushButton()
        self._chatgpt.setObjectName("primaryBtn")
        self._other_answer = QPushButton()
        self._other_answer.setObjectName("ghostBtn")
        self._chatgpt.clicked.connect(
            lambda: self.answer_settings_requested.emit(True)
        )
        self._other_answer.clicked.connect(
            lambda: self.answer_settings_requested.emit(False)
        )
        layout.addWidget(self._chatgpt)
        layout.addWidget(self._other_answer)
        layout.addStretch(1)
        return page

    def _phone_page(self) -> QWidget:
        page = self._page("onboarding.phone_title", "onboarding.phone_body")
        layout = page.layout()
        layout.takeAt(layout.count() - 1)
        self._open_phone = QPushButton()
        self._open_phone.setObjectName("ghostBtn")
        self._open_phone.clicked.connect(self.phone_settings_requested.emit)
        layout.addWidget(self._open_phone)
        layout.addStretch(1)
        return page

    def _retranslate(self) -> None:
        for label in self.findChildren(QLabel):
            key = label.property("i18nKey")
            if key:
                label.setText(i18n.t(str(key)))
                label.setAccessibleName(label.text())
        self._chatgpt.setText(i18n.t("onboarding.chatgpt"))
        self._other_answer.setText(i18n.t("onboarding.other_answer"))
        self._open_phone.setText(i18n.t("onboarding.open_phone"))
        self._skip.setText(i18n.t("onboarding.skip"))
        self._back.setText(i18n.t("onboarding.back"))
        self._sync_navigation(self._stack.currentIndex())
        for button in (
            self._chatgpt, self._other_answer, self._open_phone,
            self._skip, self._back, self._next,
        ):
            button.setAccessibleName(button.text())

    def _sync_navigation(self, index: int) -> None:
        last = index == self.PAGE_COUNT - 1
        self._back.setEnabled(index > 0)
        self._next.setText(
            i18n.t("onboarding.start") if last else i18n.t("onboarding.next")
        )
        self._next.setAccessibleName(self._next.text())
        self._progress.setText(
            i18n.t("onboarding.progress", current=index + 1, total=self.PAGE_COUNT)
        )
        self._progress.setAccessibleName(self._progress.text())

    def _go_back(self) -> None:
        self._stack.setCurrentIndex(max(0, self._stack.currentIndex() - 1))

    def _go_next(self) -> None:
        index = self._stack.currentIndex()
        if index >= self.PAGE_COUNT - 1:
            self._finish("completed")
        else:
            self._stack.setCurrentIndex(index + 1)

    def _record_handled(self, outcome: str) -> None:
        if self._handled:
            return
        self._handled = True
        if self._automatic:
            try:
                mark_onboarding_handled(outcome, self._state_path)
            except OSError:
                pass
        self.handled.emit(outcome)

    def _finish(self, outcome: str) -> None:
        self._record_handled(outcome)
        super().accept()

    def reject(self) -> None:
        self._record_handled("closed")
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt method name)
        self._record_handled("closed")
        super().closeEvent(event)


__all__ = ["OnboardingDialog"]
