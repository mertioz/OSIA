"""Reusable native desktop slideshow shell for the semantic OSIA Guide."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QElapsedTimer, QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from osia import i18n
from osia.guide_catalog import (
    TRACK_ADVANCED,
    TRACK_ESSENTIAL,
    TRACK_MASTERY,
    GuideSlide,
    localized,
    load_catalog,
    slide_by_id,
    slides_for_track,
    tracks,
)
from osia.gui.guide_visuals import GuideVisualWidget
from osia.gui.motion import MOTION, MotionController, reduced_motion_requested
from osia.gui.theme import get_theme_manager
from osia.onboarding import guide_progress, mark_onboarding_handled, update_guide_progress


class GuideSlideshowDialog(QDialog):
    """Accent-aware, accessible Guide shell with persistent direct navigation."""

    handled = pyqtSignal(str)

    ESSENTIAL_COUNT = len(slides_for_track(TRACK_ESSENTIAL))
    MASTERY_COUNT = len(slides_for_track(TRACK_MASTERY))
    ADVANCED_COUNT = len(slides_for_track(TRACK_ADVANCED))

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
        self._closing = False
        self._catalog = load_catalog("pc")
        self._progress_state = guide_progress(state_path)
        self._visited_by_track = {
            track.id: set(self._progress_state.get("visitedByTrack", {}).get(track.id, []))
            for track in self._catalog.tracks
        }
        self._last_slide_by_track = dict(self._progress_state.get("lastSlideByTrack", {}))
        self._selected_category = self._progress_state.get("lastTrackId", TRACK_ESSENTIAL)
        if self._selected_category not in {track.id for track in self._catalog.tracks}:
            self._selected_category = TRACK_ESSENTIAL
        self._current_lesson_id: str | None = None
        self._wide_layout = True
        self._rail_forced_open = False
        self._shift_wheel_clock = QElapsedTimer()
        self._theme = get_theme_manager()
        self._motion = MotionController(self, reduced_motion=lambda: reduced_motion_requested(self))

        # Compatibility views retained for callers/tests during the migration.
        self._mastery_completed = self._visited_by_track[TRACK_MASTERY]
        self._advanced_completed = self._visited_by_track[TRACK_ADVANCED]

        self.setObjectName("onboardingDialog")
        self.setWindowTitle(i18n.t("guide.window_title"))
        self.setModal(False)
        self.setMinimumSize(680, 420)
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        target_w, target_h = 1120, 740
        if available is not None:
            target_w = min(target_w, max(680, int(available.width() * 0.94)))
            target_h = min(target_h, max(420, int(available.height() * 0.90)))
        self.resize(target_w, target_h)
        self._wide_layout = target_w >= 900

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(11)
        outer.addWidget(self._build_header())

        self._body = QHBoxLayout()
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(11)
        self._rail = self._build_rail()
        self._body.addWidget(self._rail)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("guideScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.viewport().installEventFilter(self)
        self._body.addWidget(self._scroll, 1)
        outer.addLayout(self._body, 1)
        outer.addWidget(self._build_footer())

        self._install_shortcuts()
        self._theme.theme_changed.connect(self._on_theme_changed)
        i18n.add_listener(self._retranslate)
        self.destroyed.connect(lambda: i18n.remove_listener(self._retranslate))
        self._apply_style()
        self._populate_rail()
        self._sync_rail_visibility()

        if self._automatic:
            self._selected_category = TRACK_ESSENTIAL
            self._show_lesson(slides_for_track(TRACK_ESSENTIAL)[0].id, persist=True)
        else:
            self.show_home()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("guideHeader")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 9, 9, 9)
        layout.setSpacing(8)
        self._brand = QLabel("OSIA  ·  GUIDE")
        self._brand.setObjectName("guideBrand")
        layout.addWidget(self._brand)

        self._category_buttons: dict[str, QPushButton] = {}
        for track in self._catalog.tracks:
            button = self._nav_button(f"guideCategory-{track.id}")
            button.setCheckable(True)
            button.setMinimumHeight(40)
            button.clicked.connect(
                lambda _checked=False, track_id=track.id: self._select_category(track_id)
            )
            self._category_buttons[track.id] = button
            layout.addWidget(button)
        layout.addStretch(1)
        self._rail_toggle = self._nav_button("guideRailToggle")
        self._rail_toggle.clicked.connect(self._toggle_rail)
        layout.addWidget(self._rail_toggle)
        return header

    def _build_rail(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("guideRail")
        rail.setMinimumWidth(168)
        rail.setMaximumWidth(210)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(7)
        self._contents_title = QLabel()
        self._contents_title.setObjectName("guideRailTitle")
        layout.addWidget(self._contents_title)
        self._toc = QListWidget()
        self._toc.setObjectName("guideToc")
        self._toc.setWordWrap(True)
        self._toc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._toc.currentItemChanged.connect(self._on_toc_selected)
        layout.addWidget(self._toc, 1)
        return rail

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("guideFooter")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        progress_box = QVBoxLayout()
        progress_box.setSpacing(3)
        self._progress_text = QLabel()
        self._progress_text.setObjectName("guideProgressText")
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("guideProgress")
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setFixedHeight(4)
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
    def _nav_button(object_name: str, *, primary: bool = False) -> QPushButton:
        button = QPushButton()
        button.setObjectName(object_name)
        button.setProperty("guideNavigation", True)
        button.setProperty("guidePrimary", primary)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setMinimumHeight(36)
        return button

    @staticmethod
    def _text_label(text: str, object_name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setAccessibleName(text)
        return label

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

    def _track(self, track_id: str):
        return next(track for track in self._catalog.tracks if track.id == track_id)

    def _select_category(self, category_id: str) -> None:
        if category_id not in self._category_buttons:
            return
        self._selected_category = category_id
        self._populate_rail()
        resume = self._last_slide_by_track.get(category_id)
        if slide_by_id(resume) is None:
            resume = slides_for_track(category_id)[0].id
        self._show_lesson(resume, persist=True)

    def _populate_rail(self) -> None:
        self._toc.blockSignals(True)
        self._toc.clear()
        language = i18n.get_language()
        visited = self._visited_by_track[self._selected_category]
        for slide in slides_for_track(self._selected_category):
            marker = "✓" if slide.id in visited else str(slide.order)
            item = QListWidgetItem(f"{marker}  {localized(slide.title, language)}")
            item.setData(Qt.ItemDataRole.UserRole, slide.id)
            item.setToolTip(localized(slide.title, language))
            item.setSizeHint(item.sizeHint().expandedTo(self._toc.minimumSizeHint()))
            self._toc.addItem(item)
        self._toc.blockSignals(False)
        if self._current_lesson_id:
            self._select_toc_item(self._current_lesson_id)

    def _select_toc_item(self, slide_id: str) -> None:
        self._toc.blockSignals(True)
        try:
            for row in range(self._toc.count()):
                item = self._toc.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == slide_id:
                    self._toc.setCurrentRow(row)
                    return
            self._toc.setCurrentRow(-1)
        finally:
            self._toc.blockSignals(False)

    def _on_toc_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        value = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, str):
            self._show_lesson(value, persist=True)

    def _replace_content(self, widget: QWidget) -> None:
        old = self._scroll.takeWidget()
        self._scroll.setWidget(widget)
        self._scroll.verticalScrollBar().setValue(0)
        if old is not None:
            old.deleteLater()
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        self._motion.fade_in(widget, MOTION.page_ms)

    def show_home(self) -> None:
        """Show the Guide home without resetting resumable progress."""

        self._current_lesson_id = None
        self._toc.setCurrentRow(-1)
        page = QWidget()
        page.setObjectName("guideHome")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)
        layout.addWidget(self._text_label(i18n.t("guide.home_title"), "guideLessonTitle"))
        layout.addWidget(self._text_label(i18n.t("guide.home_intro"), "guideSubtitle"))
        language = i18n.get_language()
        for track in self._catalog.tracks:
            card = QPushButton()
            card.setObjectName("guideTrackCard")
            card.setProperty("trackId", track.id)
            card.setProperty("guideNavigation", True)
            card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            card.setText(
                f"{localized(track.title, language)}\n"
                f"{localized(track.summary, language)}\n"
                f"{len(track.slides)} {i18n.t('guide.contents').lower()}"
            )
            card.setAccessibleName(localized(track.title, language))
            card.setMinimumHeight(92)
            card.clicked.connect(lambda _checked=False, track_id=track.id: self._select_category(track_id))
            layout.addWidget(card)
        layout.addStretch(1)
        self._replace_content(page)
        self._sync_navigation()

    def _show_category_home(self) -> None:
        self._select_category(self._selected_category)

    def _show_lesson(self, lesson_id: str, *, persist: bool) -> None:
        slide = slide_by_id(lesson_id)
        if slide is None:
            from osia.guide_catalog import resolve_slide_id

            slide = slide_by_id(resolve_slide_id(lesson_id))
        if slide is None:
            return
        self._selected_category = slide.track_id
        self._current_lesson_id = slide.id
        self._last_slide_by_track[slide.track_id] = slide.id
        self._populate_rail()
        self._select_toc_item(slide.id)
        self._replace_content(self._build_lesson_page(slide))
        if persist:
            QTimer.singleShot(0, self._persist_progress)
        self._sync_navigation()

    def _build_lesson_page(self, slide: GuideSlide) -> QWidget:
        language = i18n.get_language()
        page = QWidget()
        page.setObjectName("guideLesson")
        slides = slides_for_track(slide.track_id)
        announcement = (
            f"{slide.order} sur {len(slides)} : {localized(slide.title, language)}"
            if language == "fr"
            else f"Slide {slide.order} of {len(slides)}: {localized(slide.title, language)}"
        )
        page.setAccessibleName(announcement)
        page.setAccessibleDescription(localized(slide.visual.alt, language))

        wide = self.width() >= 900
        self._wide_layout = wide
        root = QHBoxLayout(page) if wide else QVBoxLayout(page)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(22)
        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(12)
        text_layout.addWidget(self._text_label(
            localized(self._track(slide.track_id).title, language).upper(),
            "guideEyebrow",
        ))
        text_layout.addWidget(self._text_label(localized(slide.title, language), "guideLessonTitle"))
        text_layout.addWidget(self._text_label(localized(slide.subtitle, language), "guideSubtitle"))
        for block in slide.body_blocks:
            text_layout.addWidget(self._text_label(localized(block, language), "guideBody"))
        for callout in slide.callouts:
            card = QFrame()
            card.setObjectName("guideRememberCard" if callout.kind in {"remember", "safety"} else "guideExample")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 11, 14, 11)
            card_layout.setSpacing(5)
            card_layout.addWidget(self._text_label(localized(callout.title, language), "guideSectionTitle"))
            card_layout.addWidget(self._text_label(localized(callout.body, language), "guideBody"))
            text_layout.addWidget(card)
        text_layout.addStretch(1)
        visual = GuideVisualWidget(slide, language)
        if wide:
            root.addWidget(text, 2)
            root.addWidget(visual, 3)
        else:
            root.addWidget(visual)
            root.addWidget(text)
        return page

    def _resume_lesson_for_category(self) -> GuideSlide:
        saved = self._last_slide_by_track.get(self._selected_category)
        slide = slide_by_id(saved)
        if slide is not None and slide.track_id == self._selected_category:
            return slide
        return slides_for_track(self._selected_category)[0]

    def _persist_progress(self) -> None:
        try:
            self._progress_state = update_guide_progress(
                self._state_path,
                current_lesson=self._current_lesson_id or self._last_slide_by_track.get(self._selected_category, ""),
                last_track_id=self._selected_category,
                last_slide_by_track=self._last_slide_by_track,
                visited_by_track={key: sorted(value) for key, value in self._visited_by_track.items()},
            )
        except OSError:
            pass

    def _sync_navigation(self) -> None:
        slides = slides_for_track(self._selected_category)
        current = slide_by_id(self._current_lesson_id)
        self._skip.setVisible(self._automatic and not self._handled)
        self._back.setText(i18n.t("guide.previous"))
        self._skip.setText(i18n.t("guide.skip"))
        self._close.setText(i18n.t("guide.close"))
        self._rail_toggle.setText(i18n.t("guide.contents"))
        self._contents_title.setText(i18n.t("guide.contents"))
        self._toc.setAccessibleName(i18n.t("guide.contents"))
        for track in self._catalog.tracks:
            button = self._category_buttons[track.id]
            button.setText(localized(track.title, i18n.get_language()))
            button.setChecked(track.id == self._selected_category)
        if current is None:
            completed = len(self._visited_by_track[self._selected_category])
            self._progress_text.setText(i18n.t("guide.category_progress", current=completed, total=len(slides)))
            self._progress_bar.setValue(round(100 * completed / max(1, len(slides))))
            self._back.setEnabled(False)
            self._next.setText(i18n.t("guide.resume") if self._last_slide_by_track.get(self._selected_category) else i18n.t("guide.start"))
        else:
            index = slides.index(current)
            self._progress_text.setText(i18n.t("guide.lesson_progress", current=index + 1, total=len(slides)))
            self._progress_bar.setValue(round(100 * (index + 1) / len(slides)))
            self._back.setEnabled(index > 0)
            self._next.setText(i18n.t("guide.finish") if index == len(slides) - 1 else i18n.t("guide.next"))
        for button in (*self._category_buttons.values(), self._rail_toggle, self._skip, self._back, self._next, self._close):
            button.setAccessibleName(button.text())
        self._progress_text.setAccessibleName(self._progress_text.text())

    def _mark_current_complete(self) -> None:
        slide = slide_by_id(self._current_lesson_id)
        if slide is None:
            return
        self._visited_by_track[slide.track_id].add(slide.id)
        self._last_slide_by_track[slide.track_id] = slide.id
        self._persist_progress()
        self._populate_rail()

    def _go_back(self) -> None:
        slide = slide_by_id(self._current_lesson_id)
        if slide is None:
            return
        slides = slides_for_track(slide.track_id)
        index = slides.index(slide)
        if index > 0:
            self._show_lesson(slides[index - 1].id, persist=True)

    def _go_next(self) -> None:
        slide = slide_by_id(self._current_lesson_id)
        if slide is None:
            self._show_lesson(self._resume_lesson_for_category().id, persist=True)
            return
        slides = slides_for_track(slide.track_id)
        index = slides.index(slide)
        self._mark_current_complete()
        if index == len(slides) - 1:
            if slide.track_id == TRACK_ESSENTIAL:
                self._visited_by_track[TRACK_ESSENTIAL].update(
                    item.id for item in slides
                )
            self._finish("completed")
        else:
            self._show_lesson(slides[index + 1].id, persist=True)

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
                current_lesson=self._current_lesson_id or self._last_slide_by_track.get(self._selected_category, ""),
                last_track_id=self._selected_category,
                last_slide_by_track=self._last_slide_by_track,
                visited_by_track={key: sorted(value) for key, value in self._visited_by_track.items()},
                skipped=(outcome == "skipped" or self._progress_state.get("skipped", False)),
                finished=(outcome == "completed" or self._progress_state.get("finished", False)),
            )
        except OSError:
            pass
        self._record_handled(outcome)
        super().accept()

    def reject(self) -> None:
        self._persist_progress()
        self._record_handled("closed")
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._handled:
            self._persist_progress()
            self._record_handled("closed")
        super().closeEvent(event)

    def _toggle_rail(self) -> None:
        self._rail_forced_open = not self._rail.isVisible()
        self._sync_rail_visibility()

    def _sync_rail_visibility(self) -> None:
        narrow = self.width() < 820
        self._rail.setVisible(not narrow or self._rail_forced_open)
        self._rail_toggle.setVisible(narrow)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        next_wide = self.width() >= 900
        self._sync_rail_visibility()
        if next_wide != self._wide_layout:
            self._wide_layout = next_wide
            slide_id = self._current_lesson_id
            if slide_id:
                QTimer.singleShot(0, lambda: self._show_lesson(slide_id, persist=False))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                if self._shift_wheel_clock.isValid() and self._shift_wheel_clock.elapsed() < 320:
                    event.accept()
                    return True
                self._shift_wheel_clock.restart()
                if event.angleDelta().y() < 0:
                    self._go_next()
                elif event.angleDelta().y() > 0:
                    self._go_back()
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        nested = isinstance(self.focusWidget(), (
            QAbstractButton, QAbstractItemView, QLineEdit, QTextEdit,
            QPlainTextEdit, QComboBox, QSpinBox,
        ))
        if not nested and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            if event.key() == Qt.Key.Key_Left:
                self._go_back()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Right:
                self._go_next()
                event.accept()
                return
        super().keyPressEvent(event)

    def _retranslate(self) -> None:
        self.setWindowTitle(i18n.t("guide.window_title"))
        self._populate_rail()
        slide_id = self._current_lesson_id
        if slide_id:
            self._show_lesson(slide_id, persist=False)
        else:
            self.show_home()
        self._sync_navigation()

    @staticmethod
    def _rgba(color: QColor, alpha: int) -> str:
        return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"

    def _apply_style(self) -> None:
        palette = self._theme.palette
        primary = palette.color("primary")
        self.setStyleSheet(f"""
            QDialog#onboardingDialog, QScrollArea#guideScroll,
            QScrollArea#guideScroll > QWidget > QWidget {{
                background: {palette.background}; color: {palette.text}; border: none;
            }}
            QFrame#guideHeader, QFrame#guideFooter, QFrame#guideRail {{
                background: {palette.surface}; border: 1px solid {palette.border}; border-radius: 14px;
            }}
            QLabel#guideBrand, QLabel#guideEyebrow, QLabel#guideRailTitle {{
                color: {palette.primary}; font-size: 11px; font-weight: 800; letter-spacing: 1px;
            }}
            QLabel#guideLessonTitle {{ color: {palette.text}; font-size: 28px; font-weight: 800; }}
            QLabel#guideSubtitle {{ color: {palette.text}; font-size: 16px; font-weight: 600; }}
            QLabel#guideBody {{ color: {palette.muted_text}; font-size: 14px; }}
            QLabel#guideSectionTitle {{ color: {palette.text}; font-size: 13px; font-weight: 800; }}
            QLabel#guideProgressText, QLabel#guideVisualCaption {{ color: {palette.muted_text}; font-size: 11px; }}
            QPushButton[guideNavigation="true"] {{
                color: {palette.text}; background: {palette.elevated_surface};
                border: 1px solid {palette.border}; border-radius: 10px; padding: 7px 12px;
            }}
            QPushButton[guideNavigation="true"]:hover,
            QPushButton[guideNavigation="true"]:focus {{ border: 2px solid {palette.focus}; }}
            QPushButton[guideNavigation="true"]:checked {{
                color: {palette.primary}; background: {self._rgba(primary, 35)}; border-color: {palette.primary};
            }}
            QPushButton[guidePrimary="true"] {{
                color: {palette.background}; background: {palette.primary}; border-color: {palette.primary}; font-weight: 800;
            }}
            QPushButton[guideNavigation="true"]:disabled {{ color: {palette.disabled}; border-color: {palette.border}; }}
            QPushButton#guideTrackCard {{
                text-align: left; color: {palette.text}; background: {palette.surface};
                border: 1px solid {palette.border}; border-radius: 16px; padding: 14px 18px;
                font-size: 13px;
            }}
            QPushButton#guideTrackCard:hover, QPushButton#guideTrackCard:focus {{ border: 2px solid {palette.focus}; }}
            QListWidget#guideToc {{ background: transparent; color: {palette.muted_text}; border: none; outline: none; }}
            QListWidget#guideToc::item {{ min-height: 34px; padding: 7px; border-radius: 9px; }}
            QListWidget#guideToc::item:selected {{ color: {palette.primary}; background: {self._rgba(primary, 35)}; }}
            QProgressBar#guideProgress {{ background: {palette.elevated_surface}; border: none; border-radius: 2px; }}
            QProgressBar#guideProgress::chunk {{ background: {palette.primary}; border-radius: 2px; }}
            QFrame#guideExample {{ background: {palette.surface}; border: 1px solid {palette.border}; border-radius: 13px; }}
            QFrame#guideRememberCard {{ background: {self._rgba(primary, 24)}; border: 1px solid {palette.primary}; border-radius: 13px; }}
            QFrame#guideVisual {{ background: {palette.surface}; border: 1px solid {palette.border}; border-radius: 22px; }}
            QLabel#guideVisualBadge {{ min-width: 72px; min-height: 72px; border-radius: 36px;
                background: {self._rgba(primary, 32)}; color: {palette.primary}; font-size: 34px; font-weight: 800; }}
            QLabel#guideVisualTitle {{ color: {palette.text}; font-size: 19px; font-weight: 800; }}
            QLabel#guideVisualNode {{ color: {palette.muted_text}; background: {palette.elevated_surface};
                border: 1px solid {palette.border}; border-radius: 12px; padding: 8px; }}
            QLabel#guideVisualNode[active="true"] {{ color: {palette.primary}; border-color: {palette.primary}; }}
            QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: {palette.border}; min-height: 28px; border-radius: 4px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

    def _on_theme_changed(self, _key: str) -> None:
        self._apply_style()


__all__ = ["GuideSlideshowDialog"]
