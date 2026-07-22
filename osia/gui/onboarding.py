"""Responsive native Qt interface for the complete offline OSIA guide."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from osia import i18n
from osia.guide_content import (
    CATEGORIES,
    CATEGORY_ADVANCED,
    CATEGORY_ESSENTIAL,
    CATEGORY_MASTERY,
    LESSONS_BY_ID,
    GuideLesson,
    lessons_for_category,
    localized,
)
from osia.onboarding import (
    guide_progress,
    mark_onboarding_handled,
    update_guide_progress,
)
from osia.utils.assets import asset_path


class _ScaledPixmapLabel(QLabel):
    """A bounded, aspect-preserving screenshot surface."""

    def __init__(self, pixmap: QPixmap, alt_text: str, parent=None) -> None:
        super().__init__(parent)
        self._source = pixmap
        self.setObjectName("guideImage")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.setMinimumHeight(150)
        self.setMaximumHeight(320)
        self.setAccessibleName(alt_text)
        self.setAccessibleDescription(alt_text)
        self.setToolTip(alt_text)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt method name)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        if self._source.isNull() or self._source.width() <= 0:
            return 180
        natural = round(width * self._source.height() / self._source.width())
        return max(150, min(320, natural))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._source.isNull():
            return
        target = self.size()
        self.setPixmap(
            self._source.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


class OnboardingDialog(QDialog):
    """Integrated guide with three categories and resumable offline lessons."""

    handled = pyqtSignal(str)

    ESSENTIAL_COUNT = len(lessons_for_category(CATEGORY_ESSENTIAL))
    MASTERY_COUNT = len(lessons_for_category(CATEGORY_MASTERY))
    ADVANCED_COUNT = len(lessons_for_category(CATEGORY_ADVANCED))

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        automatic: bool = False,
        state_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._automatic = bool(automatic)
        self._state_path = state_path
        self._handled = False
        self._progress_state = guide_progress(state_path)
        self._mastery_completed = set(
            self._progress_state.get("masteryCompleted", [])
        )
        self._advanced_completed = set(
            self._progress_state.get("advancedCompleted", [])
        )
        self._selected_category = CATEGORY_ESSENTIAL
        self._current_lesson_id: str | None = None

        self.setObjectName("onboardingDialog")
        self.setWindowTitle(i18n.t("guide.window_title"))
        self.setModal(False)
        self.setMinimumSize(680, 420)
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        target_w, target_h = 1040, 710
        if available is not None:
            target_w = min(target_w, max(680, int(available.width() * 0.92)))
            target_h = min(target_h, max(420, int(available.height() * 0.90)))
        self.resize(target_w, target_h)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        body.addWidget(self._build_sidebar())

        self._scroll = QScrollArea()
        self._scroll.setObjectName("guideScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        body.addWidget(self._scroll, 1)
        outer.addLayout(body, 1)

        outer.addWidget(self._build_footer())
        self._install_shortcuts()
        self._apply_style()

        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))

        self._populate_toc()
        if self._automatic:
            first = lessons_for_category(CATEGORY_ESSENTIAL)[0]
            self._show_lesson(first.id, persist=True)
        else:
            self.show_home()

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("guideSidebar")
        sidebar.setMinimumWidth(205)
        sidebar.setMaximumWidth(245)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(8)

        self._category_group = QButtonGroup(self)
        self._category_group.setExclusive(True)
        self._category_buttons: dict[str, QPushButton] = {}
        for category in CATEGORIES:
            button = self._nav_button(f"guideCategory-{category.id}")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, category_id=category.id:
                self._select_category(category_id)
            )
            self._category_group.addButton(button)
            self._category_buttons[category.id] = button
            layout.addWidget(button)
        self._category_buttons[CATEGORY_ESSENTIAL].setChecked(True)

        self._contents_title = self._label("guideContentsTitle")
        layout.addWidget(self._contents_title)
        self._toc = QListWidget()
        self._toc.setObjectName("guideToc")
        self._toc.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._toc.setWordWrap(True)
        self._toc.setAccessibleName(i18n.t("guide.contents"))
        self._toc.currentItemChanged.connect(self._on_toc_selected)
        layout.addWidget(self._toc, 1)
        return sidebar

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("guideFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        progress_box = QVBoxLayout()
        progress_box.setSpacing(3)
        self._progress_text = self._label("guideProgressText")
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("guideProgress")
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setFixedHeight(6)
        progress_box.addWidget(self._progress_text)
        progress_box.addWidget(self._progress_bar)
        layout.addLayout(progress_box, 1)

        self._skip = self._nav_button("guideSkip")
        self._back = self._nav_button("guideBack")
        self._next = self._nav_button("guideNext", primary=True)
        self._close = self._nav_button("guideClose")
        self._skip.clicked.connect(lambda: self._finish("skipped"))
        self._back.clicked.connect(self._go_back)
        self._next.clicked.connect(self._go_next)
        self._close.clicked.connect(self.reject)
        layout.addWidget(self._skip)
        layout.addWidget(self._back)
        layout.addWidget(self._next)
        layout.addWidget(self._close)
        return footer

    @staticmethod
    def _label(object_name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _nav_button(object_name: str, *, primary: bool = False) -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        button.setProperty("guideNavigation", True)
        button.setProperty("guidePrimary", primary)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _install_shortcuts(self) -> None:
        self._previous_shortcut = QShortcut(QKeySequence("Alt+Left"), self)
        self._previous_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._previous_shortcut.activated.connect(self._go_back)
        self._next_shortcut = QShortcut(QKeySequence("Alt+Right"), self)
        self._next_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._next_shortcut.activated.connect(self._go_next)
        self._home_shortcut = QShortcut(QKeySequence("Ctrl+Home"), self)
        self._home_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._home_shortcut.activated.connect(self.show_home)

    def _category_title(self, category_id: str) -> str:
        category = next(item for item in CATEGORIES if item.id == category_id)
        return localized(category.title, i18n.get_language())

    def _category_summary(self, category_id: str) -> str:
        category = next(item for item in CATEGORIES if item.id == category_id)
        return localized(category.summary, i18n.get_language())

    def _select_category(self, category_id: str) -> None:
        if category_id not in self._category_buttons:
            return
        self._selected_category = category_id
        self._category_buttons[category_id].setChecked(True)
        self._populate_toc()
        self._show_category_home()

    def _populate_toc(self) -> None:
        self._toc.blockSignals(True)
        self._toc.clear()
        language = i18n.get_language()
        for lesson in lessons_for_category(self._selected_category):
            item = QListWidgetItem(
                f"{lesson.order}. {localized(lesson.title, language)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, lesson.id)
            item.setToolTip(localized(lesson.title, language))
            self._toc.addItem(item)
        self._toc.blockSignals(False)
        if self._current_lesson_id:
            self._select_toc_item(self._current_lesson_id)

    def _select_toc_item(self, lesson_id: str) -> None:
        self._toc.blockSignals(True)
        try:
            for row in range(self._toc.count()):
                item = self._toc.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == lesson_id:
                    self._toc.setCurrentRow(row)
                    break
            else:
                self._toc.setCurrentRow(-1)
        finally:
            self._toc.blockSignals(False)

    def _on_toc_selected(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        lesson_id = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(lesson_id, str):
            self._show_lesson(lesson_id, persist=True)

    def _replace_content(self, widget: QWidget) -> None:
        old = self._scroll.takeWidget()
        self._scroll.setWidget(widget)
        self._scroll.verticalScrollBar().setValue(0)
        if old is not None:
            old.deleteLater()

    def show_home(self) -> None:
        """Show the guide home/table of contents without resetting progress."""

        self._current_lesson_id = None
        self._toc.blockSignals(True)
        self._toc.setCurrentRow(-1)
        self._toc.blockSignals(False)
        page = QWidget()
        page.setObjectName("guideHome")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        title = self._text_label(i18n.t("guide.home_title"), "guideLessonTitle")
        intro = self._text_label(i18n.t("guide.home_intro"), "guideIntro")
        layout.addWidget(title)
        layout.addWidget(intro)
        for category in CATEGORIES:
            card = QFrame()
            card.setObjectName("guideCategoryCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(15, 12, 15, 12)
            card_layout.setSpacing(5)
            card_layout.addWidget(
                self._text_label(
                    localized(category.title, i18n.get_language()),
                    "guideSectionTitle",
                )
            )
            card_layout.addWidget(
                self._text_label(
                    localized(category.summary, i18n.get_language()),
                    "guideBody",
                )
            )
            count = len(lessons_for_category(category.id))
            card_layout.addWidget(
                self._text_label(
                    i18n.t("guide.lesson_count", count=count),
                    "guideMeta",
                )
            )
            layout.addWidget(card)
        layout.addWidget(
            self._text_label(i18n.t("guide.home_hint"), "guideRemember")
        )
        layout.addStretch(1)
        self._replace_content(page)
        self._sync_navigation()

    def _show_category_home(self) -> None:
        self._current_lesson_id = None
        page = QWidget()
        page.setObjectName("guideCategoryHome")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(
            self._text_label(
                self._category_title(self._selected_category),
                "guideLessonTitle",
            )
        )
        layout.addWidget(
            self._text_label(
                self._category_summary(self._selected_category),
                "guideIntro",
            )
        )
        layout.addWidget(
            self._text_label(i18n.t("guide.category_hint"), "guideRemember")
        )
        layout.addStretch(1)
        self._replace_content(page)
        self._sync_navigation()

    @staticmethod
    def _text_label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setAccessibleName(text)
        return label

    def _show_lesson(self, lesson_id: str, *, persist: bool) -> None:
        lesson = LESSONS_BY_ID.get(lesson_id)
        if lesson is None:
            return
        self._selected_category = lesson.category
        self._category_buttons[lesson.category].setChecked(True)
        self._current_lesson_id = lesson.id
        self._populate_toc()
        self._select_toc_item(lesson.id)
        self._replace_content(self._build_lesson_page(lesson))
        if persist:
            self._progress_state = update_guide_progress(
                self._state_path,
                current_lesson=lesson.id,
                mastery_completed=sorted(self._mastery_completed),
                advanced_completed=sorted(self._advanced_completed),
            )
        self._sync_navigation()

    def _build_lesson_page(self, lesson: GuideLesson) -> QWidget:
        language = i18n.get_language()
        page = QWidget()
        page.setObjectName("guideLesson")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(12)

        eyebrow = self._text_label(
            self._category_title(lesson.category), "guideEyebrow"
        )
        title = self._text_label(
            localized(lesson.title, language), "guideLessonTitle"
        )
        intro = self._text_label(
            localized(lesson.intro, language), "guideIntro"
        )
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(intro)

        for paragraph in lesson.paragraphs:
            layout.addWidget(
                self._text_label(localized(paragraph, language), "guideBody")
            )

        if lesson.asset:
            image_path = Path(asset_path(lesson.asset))
            alt = localized(lesson.alt_text, language)
            image_frame = QFrame()
            image_frame.setObjectName("guideImageFrame")
            image_layout = QVBoxLayout(image_frame)
            image_layout.setContentsMargins(10, 10, 10, 8)
            image_layout.setSpacing(7)
            pixmap = QPixmap(str(image_path)) if image_path.is_file() else QPixmap()
            if pixmap.isNull():
                missing = self._text_label(
                    i18n.t("guide.image_unavailable") + " " + alt,
                    "guideImageMissing",
                )
                missing.setAccessibleDescription(alt)
                image_layout.addWidget(missing)
            else:
                image_layout.addWidget(_ScaledPixmapLabel(pixmap, alt))
            caption = self._text_label(
                localized(lesson.caption, language), "guideCaption"
            )
            caption.setAccessibleDescription(alt)
            image_layout.addWidget(caption)
            layout.addWidget(image_frame)

        example = QFrame()
        example.setObjectName("guideExample")
        example_layout = QVBoxLayout(example)
        example_layout.setContentsMargins(14, 11, 14, 11)
        example_layout.setSpacing(5)
        example_layout.addWidget(
            self._text_label(
                localized(lesson.example_title, language),
                "guideSectionTitle",
            )
        )
        example_layout.addWidget(
            self._text_label(localized(lesson.example, language), "guideBody")
        )
        layout.addWidget(example)

        if lesson.remember:
            remember = QFrame()
            remember.setObjectName("guideRememberCard")
            remember_layout = QVBoxLayout(remember)
            remember_layout.setContentsMargins(14, 10, 14, 10)
            remember_layout.setSpacing(4)
            remember_layout.addWidget(
                self._text_label(i18n.t("guide.remember"), "guideSectionTitle")
            )
            remember_layout.addWidget(
                self._text_label(
                    localized(lesson.remember, language), "guideBody"
                )
            )
            layout.addWidget(remember)

        layout.addStretch(1)
        return page

    def _resume_lesson_for_category(self) -> GuideLesson:
        saved = self._progress_state.get("currentLesson", "")
        lesson = LESSONS_BY_ID.get(saved)
        if lesson is not None and lesson.category == self._selected_category:
            return lesson
        return lessons_for_category(self._selected_category)[0]

    def _sync_navigation(self) -> None:
        lessons = lessons_for_category(self._selected_category)
        current = LESSONS_BY_ID.get(self._current_lesson_id or "")
        self._skip.setVisible(self._automatic and not self._handled)
        if current is None:
            if self._selected_category == CATEGORY_MASTERY:
                completed = len(self._mastery_completed)
            elif self._selected_category == CATEGORY_ADVANCED:
                completed = len(self._advanced_completed)
            else:
                completed = (
                    len(lessons)
                    if self._progress_state.get("essentialCompleted") else 0
                )
            self._progress_text.setText(
                i18n.t("guide.category_progress", current=completed, total=len(lessons))
            )
            self._progress_bar.setValue(round(100 * completed / max(1, len(lessons))))
            self._back.setEnabled(False)
            saved = self._progress_state.get("currentLesson", "")
            saved_lesson = LESSONS_BY_ID.get(saved)
            key = (
                "guide.resume"
                if saved_lesson is not None
                and saved_lesson.category == self._selected_category
                else "guide.start"
            )
            self._next.setText(i18n.t(key))
        else:
            index = lessons.index(current)
            self._progress_text.setText(
                i18n.t("guide.lesson_progress", current=index + 1, total=len(lessons))
            )
            self._progress_bar.setValue(round(100 * (index + 1) / len(lessons)))
            self._back.setEnabled(index > 0)
            self._next.setText(
                i18n.t("guide.finish")
                if index == len(lessons) - 1
                else i18n.t("guide.next")
            )

        self._back.setText(i18n.t("guide.previous"))
        self._skip.setText(i18n.t("guide.skip"))
        self._close.setText(i18n.t("guide.close"))
        for button in (
            *self._category_buttons.values(),
            self._skip,
            self._back,
            self._next,
            self._close,
        ):
            button.setAccessibleName(button.text())
        self._progress_text.setAccessibleName(self._progress_text.text())

    def _mark_current_complete(self) -> None:
        lesson = LESSONS_BY_ID.get(self._current_lesson_id or "")
        if lesson is None:
            return
        if lesson.category == CATEGORY_MASTERY:
            self._mastery_completed.add(lesson.id)
        elif lesson.category == CATEGORY_ADVANCED:
            self._advanced_completed.add(lesson.id)
        self._progress_state = update_guide_progress(
            self._state_path,
            current_lesson=lesson.id,
            essential_completed=(
                self._progress_state.get("essentialCompleted", False)
                or lesson.id == "essential.ready"
            ),
            mastery_completed=sorted(self._mastery_completed),
            advanced_completed=sorted(self._advanced_completed),
        )

    def _go_back(self) -> None:
        lesson = LESSONS_BY_ID.get(self._current_lesson_id or "")
        if lesson is None:
            return
        lessons = lessons_for_category(lesson.category)
        index = lessons.index(lesson)
        if index > 0:
            self._show_lesson(lessons[index - 1].id, persist=True)

    def _go_next(self) -> None:
        lesson = LESSONS_BY_ID.get(self._current_lesson_id or "")
        if lesson is None:
            self._show_lesson(
                self._resume_lesson_for_category().id,
                persist=True,
            )
            return
        lessons = lessons_for_category(lesson.category)
        index = lessons.index(lesson)
        self._mark_current_complete()
        if index == len(lessons) - 1:
            self._finish("completed")
        else:
            self._show_lesson(lessons[index + 1].id, persist=True)

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
        try:
            self._progress_state = update_guide_progress(
                self._state_path,
                current_lesson=self._current_lesson_id or self._progress_state.get("currentLesson", ""),
                essential_completed=self._progress_state.get("essentialCompleted", False),
                mastery_completed=sorted(self._mastery_completed),
                advanced_completed=sorted(self._advanced_completed),
                skipped=(outcome == "skipped" or self._progress_state.get("skipped", False)),
                finished=(outcome == "completed" or self._progress_state.get("finished", False)),
            )
        except OSError:
            pass
        self._record_handled(outcome)
        super().accept()

    def reject(self) -> None:
        try:
            self._progress_state = update_guide_progress(
                self._state_path,
                current_lesson=self._current_lesson_id or self._progress_state.get("currentLesson", ""),
                mastery_completed=sorted(self._mastery_completed),
                advanced_completed=sorted(self._advanced_completed),
            )
        except OSError:
            pass
        self._record_handled("closed")
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt method name)
        if not self._handled:
            try:
                self._progress_state = update_guide_progress(
                    self._state_path,
                    current_lesson=self._current_lesson_id or self._progress_state.get("currentLesson", ""),
                    mastery_completed=sorted(self._mastery_completed),
                    advanced_completed=sorted(self._advanced_completed),
                )
            except OSError:
                pass
            self._record_handled("closed")
        super().closeEvent(event)

    def _retranslate(self) -> None:
        self.setWindowTitle(i18n.t("guide.window_title"))
        self._contents_title.setText(i18n.t("guide.contents"))
        self._toc.setAccessibleName(i18n.t("guide.contents"))
        for category in CATEGORIES:
            button = self._category_buttons[category.id]
            button.setText(localized(category.title, i18n.get_language()))
        self._populate_toc()
        lesson_id = self._current_lesson_id
        if lesson_id:
            self._show_lesson(lesson_id, persist=False)
        elif self._scroll.widget() is not None and self._scroll.widget().objectName() == "guideHome":
            self.show_home()
        else:
            self._show_category_home()
        self._sync_navigation()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog#onboardingDialog {
                background: #100d18;
                color: #f4f0ff;
            }
            QFrame#guideFooter, QFrame#guideSidebar {
                background: #191424;
                border: 1px solid #332744;
                border-radius: 14px;
            }
            QLabel#guideMeta, QLabel#guideProgressText {
                color: #aaa1b8; font-size: 12px;
            }
            QLabel#guideLessonTitle { font-size: 25px; font-weight: 700; color: #ffffff; }
            QLabel#guideEyebrow { color: #b79aff; font-size: 12px; font-weight: 700; }
            QLabel#guideIntro { color: #e7def6; font-size: 15px; line-height: 1.35; }
            QLabel#guideBody { color: #d2c9df; font-size: 13px; line-height: 1.35; }
            QLabel#guideSectionTitle { color: #f4efff; font-size: 14px; font-weight: 700; }
            QLabel#guideCaption { color: #9f96ac; font-size: 11px; font-style: italic; }
            QLabel#guideImageMissing { color: #b8afc4; padding: 18px; }
            QFrame#guideCategoryCard, QFrame#guideExample, QFrame#guideImageFrame {
                background: #191424;
                border: 1px solid #332744;
                border-radius: 12px;
            }
            QFrame#guideRememberCard {
                background: #211936;
                border: 1px solid #694fa0;
                border-radius: 12px;
            }
            QListWidget#guideToc {
                background: transparent;
                border: none;
                color: #cfc6da;
                outline: none;
            }
            QListWidget#guideToc::item { padding: 8px 7px; border-radius: 8px; }
            QListWidget#guideToc::item:selected { background: #4e3674; color: #ffffff; }
            QPushButton[guideNavigation="true"] {
                background: #241c31;
                color: #ddd4ea;
                border: 1px solid #443455;
                border-radius: 9px;
                padding: 7px 11px;
                min-height: 18px;
            }
            QPushButton[guideNavigation="true"]:hover { border-color: #9370d5; }
            QPushButton[guideNavigation="true"]:focus { border: 2px solid #c2a5ff; }
            QPushButton[guideNavigation="true"]:checked { background: #4e3674; color: white; }
            QPushButton[guidePrimary="true"] { background: #7250b5; color: white; border-color: #8f6fd0; }
            QPushButton[guideNavigation="true"]:disabled { color: #70697a; border-color: #2c2534; }
            QProgressBar#guideProgress { background: #2a2234; border: none; border-radius: 3px; }
            QProgressBar#guideProgress::chunk { background: #956fe1; border-radius: 3px; }
            QScrollArea#guideScroll { background: #100d18; border: none; }
            QScrollArea#guideScroll > QWidget > QWidget { background: #100d18; }
            QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
            QScrollBar::handle:vertical { background: #4a3b59; min-height: 28px; border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """
        )
        self._retranslate()


__all__ = ["OnboardingDialog"]
