"""WOR-151 complete-guide content, state, UI, asset and packaging regressions."""

from __future__ import annotations

import json
import os
import re
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

from osia import i18n, onboarding
from osia.guide_content import (
    CATEGORIES,
    CATEGORY_ADVANCED,
    CATEGORY_ESSENTIAL,
    CATEGORY_MASTERY,
    GUIDE_CONTENT_SCHEMA_VERSION,
    LESSONS,
    LESSONS_BY_ID,
    lessons_for_category,
)
from osia.gui import main_popup
from osia.gui.onboarding import OnboardingDialog, _ScaledPixmapLabel
from osia.utils.assets import asset_path


ROOT = Path(__file__).resolve().parent.parent
_QT_APP = QApplication.instance() or QApplication([])
EXPECTED_ASSETS = {
    "settings-api-provider.png",
    "settings-chatgpt-account.png",
    "settings-menu.png",
    "settings-mobile-pairing.png",
    "settings-model-hierarchy.png",
    "settings-voice.png",
}
AUDITED_SETTINGS_PAGES = (
    "General",
    "Models",
    "Voice",
    "Wake word",
    "Memory",
    "MCP / Connectors",
    "Connection / Mobile",
    "Advanced",
    "Updates",
    "Legal",
)
AUDITED_SETTINGS_KEYS = (
    "settings.cat.general",
    "settings.cat.models",
    "settings.cat.voice",
    "settings.cat.wake",
    "settings.cat.memory",
    "settings.cat.mcp",
    "settings.cat.mobile",
    "settings.cat.advanced",
    "settings.cat.updates",
    "settings.cat.legal",
)


def _english_copy(lessons=LESSONS) -> str:
    pieces: list[str] = []
    for lesson in lessons:
        for value in (
            lesson.title,
            lesson.intro,
            *lesson.paragraphs,
            lesson.example_title,
            lesson.example,
            lesson.remember,
            lesson.caption,
            lesson.alt_text,
        ):
            if value:
                pieces.append(value["en"])
    return "\n".join(pieces)


def test_clean_install_is_claimed_once_and_restart_does_not_reopen(tmp_path):
    marker = tmp_path / "profile" / "onboarding.json"
    assert onboarding.create_fresh_install_marker(marker)
    assert onboarding.onboarding_state(marker)["status"] == "pending"
    assert onboarding.claim_automatic_onboarding(marker)
    state = onboarding.onboarding_state(marker)
    assert state["status"] == "presenting"
    assert state["contentSchemaVersion"] == GUIDE_CONTENT_SCHEMA_VERSION
    assert not onboarding.claim_automatic_onboarding(marker)


def test_existing_users_and_updates_are_not_opted_in(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text("{}", encoding="utf-8")
    marker = profile / "onboarding.json"
    assert not onboarding.create_fresh_install_marker(marker)
    assert not marker.exists()
    assert not onboarding.claim_automatic_onboarding(marker)
    state = onboarding.onboarding_state(marker)
    assert state["status"] == "handled"
    assert state["outcome"] == "migrated"
    assert state["essentialCompleted"] is False


@pytest.mark.parametrize("outcome", ["completed", "skipped", "closed"])
def test_handled_outcomes_persist_across_app_version_changes(tmp_path, outcome):
    marker = tmp_path / "onboarding.json"
    onboarding.mark_onboarding_handled(outcome, marker)
    state = onboarding.onboarding_state(marker)
    assert state["schemaVersion"] == onboarding.ONBOARDING_STATE_VERSION
    assert state["status"] == "handled"
    assert state["outcome"] == outcome
    assert state["contentSchemaVersion"] == GUIDE_CONTENT_SCHEMA_VERSION
    assert not onboarding.claim_automatic_onboarding(marker)


def test_old_v1_state_gains_progress_without_resetting_or_reopening(tmp_path):
    marker = tmp_path / "onboarding.json"
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": "handled",
                "outcome": "completed",
                "contentSchemaVersion": 1,
                "currentLesson": "advanced.voice-tts-stt",
                "advancedCompleted": ["advanced.pc-mobile-connection"],
            }
        ),
        encoding="utf-8",
    )
    progress = onboarding.guide_progress(marker)
    assert progress["contentSchemaVersion"] == GUIDE_CONTENT_SCHEMA_VERSION
    assert progress["currentLesson"] == "advanced.voice-tts-stt"
    assert progress["masteryCompleted"] == ["advanced.pc-mobile-connection"]
    assert progress["advancedCompleted"] == []
    onboarding.update_guide_progress(marker, finished=True)
    state = onboarding.onboarding_state(marker)
    assert state["outcome"] == "completed"
    assert state["currentLesson"] == "advanced.voice-tts-stt"
    assert not onboarding.claim_automatic_onboarding(marker)


def test_corrupt_progress_is_bounded_to_registered_lesson_ids(tmp_path):
    marker = tmp_path / "onboarding.json"
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": "handled",
                "outcome": "completed",
                "currentLesson": "unknown.lesson",
                "advancedCompleted": [
                    "advanced.voice-tts-stt",
                    "advanced.voice-tts-stt",
                    "essential.ready",
                    "unknown.lesson",
                    3,
                ],
            }
        ),
        encoding="utf-8",
    )
    progress = onboarding.guide_progress(marker)
    assert progress["currentLesson"] == ""
    assert progress["masteryCompleted"] == ["advanced.voice-tts-stt"]
    assert progress["advancedCompleted"] == []


def test_v2_advanced_progress_migrates_to_mastery_only(tmp_path):
    marker = tmp_path / "onboarding.json"
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "status": "handled",
                "outcome": "completed",
                "contentSchemaVersion": 2,
                "essentialCompleted": True,
                "currentLesson": "advanced.ai-model-hierarchy",
                "advancedCompleted": ["advanced.ai-model-hierarchy"],
                "finished": True,
            }
        ),
        encoding="utf-8",
    )
    progress = onboarding.guide_progress(marker)
    assert progress["essentialCompleted"] is True
    assert progress["currentLesson"] == "advanced.ai-model-hierarchy"
    assert progress["masteryCompleted"] == ["advanced.ai-model-hierarchy"]
    assert progress["advancedCompleted"] == []
    assert progress["finished"] is True
    assert onboarding.onboarding_state(marker)["outcome"] == "completed"


def test_installer_marks_only_fresh_non_update_profiles():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    batch = (ROOT / "install.bat").read_text("utf-8")
    assert "FreshInstallOnboardingEligible" in script
    assert "not DirExists(ExpandConstant('{userprofile}\\.osia'))" in script
    assert "not RegKeyExists(HKCU, UninstallKey)" in script
    assert "FreshInstallOnboardingEligible and (not IsUpdateMode)" in script
    assert "onboarding.json" in script
    assert "OSIA_HAD_STATE" in batch
    assert 'if exist "%VENV_PY%" set "OSIA_HAD_STATE=1"' in batch
    assert "create_fresh_install_marker" in batch


def test_frozen_smoke_covers_complete_guide_and_assets():
    launcher = (
        ROOT / "packaging" / "windows" / "launcher.py"
    ).read_text(encoding="utf-8")
    spec = (ROOT / "packaging" / "windows" / "osia.spec").read_text("utf-8")
    assert "create_fresh_install_marker(state_path)" in launcher
    assert 'mark_onboarding_handled("completed", state_path)' in launcher
    assert "OnboardingDialog(automatic=False)" in launcher
    assert "ESSENTIAL_COUNT < 7" in launcher
    assert "MASTERY_COUNT < 16" in launcher
    assert "ADVANCED_COUNT < 38" in launcher
    assert "guide asset" in launcher
    assert '(str(ROOT / "assets"), "assets")' in spec


def test_three_categories_and_all_stable_ids_are_unique():
    assert [category.id for category in CATEGORIES] == [
        CATEGORY_ESSENTIAL,
        CATEGORY_MASTERY,
        CATEGORY_ADVANCED,
    ]
    ids = [lesson.id for lesson in LESSONS]
    assert len(ids) == len(set(ids)) == 66
    assert set(ids) == set(LESSONS_BY_ID)
    assert len(lessons_for_category(CATEGORY_ESSENTIAL)) == 8
    assert len(lessons_for_category(CATEGORY_MASTERY)) == 20
    assert len(lessons_for_category(CATEGORY_ADVANCED)) == 38
    for category in CATEGORIES:
        orders = [lesson.order for lesson in lessons_for_category(category.id)]
        assert orders == list(range(1, len(orders) + 1))


def test_decorative_guide_header_is_removed_without_reserved_space(tmp_path):
    source = (ROOT / "osia" / "gui" / "onboarding.py").read_text("utf-8")
    assert "def _build_header" not in source
    assert "outer.addWidget(self._build_header())" not in source
    assert "guideHeader" not in source
    assert "guideTitle" not in source
    assert "guideSubtitle" not in source
    dialog = OnboardingDialog(state_path=tmp_path / "state.json")
    assert dialog.findChild(QFrame, "guideHeader") is None
    body_top = dialog._toc.mapTo(dialog, dialog._toc.rect().topLeft()).y()
    assert body_top < 170
    dialog.deleteLater()


def test_mastery_and_developer_progress_are_independent(tmp_path):
    marker = tmp_path / "state.json"
    progress = onboarding.update_guide_progress(
        marker,
        mastery_completed=["advanced.ai-model-hierarchy"],
        advanced_completed=["advanced.handshake"],
    )
    assert progress["masteryCompleted"] == ["advanced.ai-model-hierarchy"]
    assert progress["advancedCompleted"] == ["advanced.handshake"]
    restored = onboarding.guide_progress(marker)
    assert restored == progress


def test_essential_content_covers_required_beginner_topics_and_every_menu_page():
    copy = _english_copy(lessons_for_category(CATEGORY_ESSENTIAL)).lower()
    for phrase in (
        "api key",
        "chatgpt account",
        "pairing token",
        "first useful requests",
        "goal + context + expected result",
        "summarize this document",
    ):
        assert phrase in copy
    for page in AUDITED_SETTINGS_PAGES:
        assert page.lower() in copy


def test_menu_lesson_matches_the_actual_settings_page_registry():
    source = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
    for key, label in zip(AUDITED_SETTINGS_KEYS, AUDITED_SETTINGS_PAGES):
        assert f'("{key}"' in source or f'("{key}",' in source
        assert i18n.TRANSLATIONS[key]["en"] == label


def test_mastery_content_covers_only_audited_runtime_capabilities():
    copy = _english_copy(lessons_for_category(CATEGORY_MASTERY)).lower()
    for phrase in (
        "private-lan wss",
        "webrtc/p2p",
        "end-to-end encrypted relay",
        "tts",
        "stt",
        "provider and api hierarchy",
        "models and ai hierarchy",
        "memory and context",
        "scheduled tasks",
        "updates",
    ):
        assert phrase in copy
    assert "live api is available" not in copy
    assert "free and unlimited" not in copy
    assert "works while the pc is off" not in copy
    assert "always prefers chatgpt" not in copy


def test_advanced_content_covers_architecture_takeover_and_release():
    advanced = lessons_for_category(CATEGORY_ADVANCED)
    subjects = {lesson.subject_id for lesson in advanced}
    assert len(subjects) == 38
    for subject in (
        "repository-topology",
        "pairing-protocol",
        "ice-stun-turn",
        "mcp",
        "takeover-workflow",
        "windows-packaging",
        "android-packaging",
        "security-model",
        "glossary",
    ):
        assert subject in subjects
    glossary = next(lesson for lesson in advanced if lesson.subject_id == "glossary")
    assert set(glossary.glossary_ids) == {
        "provider", "model", "api", "token", "context", "thinking", "mcp",
        "dcr", "oauth", "cron", "websocket", "e2e", "lan", "p2p", "webrtc",
        "sdp", "ice", "stun", "turn", "signaling", "relay", "durable-object",
        "hibernation", "chunking", "hash", "mime", "abi", "apk", "aab", "r8",
        "pyinstaller", "inno-setup", "r2", "rollout", "version-name",
        "version-code",
    }


def test_every_lesson_has_complete_english_and_french_copy():
    for lesson in LESSONS:
        assert lesson.id.startswith(("essential.", "mastery.", "advanced."))
        for value in (
            lesson.title,
            lesson.intro,
            *lesson.paragraphs,
            lesson.example_title,
            lesson.example,
        ):
            assert value.get("en", "").strip()
            assert value.get("fr", "").strip()
        assert 1 <= len(lesson.paragraphs) <= 5
        if lesson.asset:
            assert lesson.caption and lesson.alt_text
            assert lesson.caption.get("en") and lesson.caption.get("fr")
            assert lesson.alt_text.get("en") and lesson.alt_text.get("fr")


def test_no_content_level_configuration_or_application_action_buttons(tmp_path):
    dialog = OnboardingDialog(state_path=tmp_path / "state.json")
    buttons = dialog.findChildren(QPushButton)
    assert buttons
    assert all(button.property("guideNavigation") is True for button in buttons)
    forbidden = {
        "connect chatgpt",
        "open models",
        "open mobile",
        "go to settings",
        "configure provider",
        "start pairing",
    }
    assert not forbidden.intersection(button.text().strip().lower() for button in buttons)
    assert not hasattr(dialog, "answer_settings_requested")
    assert not hasattr(dialog, "phone_settings_requested")
    parent_source = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
    assert "dialog.answer_settings_requested.connect" not in parent_source
    assert "dialog.phone_settings_requested.connect" not in parent_source
    dialog.deleteLater()


def test_toc_category_previous_and_next_navigation(tmp_path):
    dialog = OnboardingDialog(state_path=tmp_path / "state.json")
    assert dialog._current_lesson_id is None
    assert dialog._toc.count() == 8
    assert dialog._next.text() == "Start"
    QTest.mouseClick(dialog._next, Qt.MouseButton.LeftButton)
    assert dialog._current_lesson_id == "essential.what-is-osia"
    assert not dialog._back.isEnabled()
    QTest.mouseClick(dialog._next, Qt.MouseButton.LeftButton)
    assert dialog._current_lesson_id == "essential.answer-methods"
    assert dialog._back.isEnabled()
    QTest.mouseClick(dialog._back, Qt.MouseButton.LeftButton)
    assert dialog._current_lesson_id == "essential.what-is-osia"
    QTest.mouseClick(
        dialog._category_buttons[CATEGORY_MASTERY], Qt.MouseButton.LeftButton
    )
    assert dialog._selected_category == CATEGORY_MASTERY
    assert dialog._current_lesson_id is None
    dialog._toc.setCurrentRow(4)
    assert dialog._current_lesson_id == "advanced.ai-model-hierarchy"
    dialog.deleteLater()


def test_skip_persists_and_french_ui_is_available(tmp_path):
    marker = tmp_path / "state.json"
    onboarding.create_fresh_install_marker(marker)
    assert onboarding.claim_automatic_onboarding(marker)
    dialog = OnboardingDialog(automatic=True, state_path=marker)
    handled = QSignalSpy(dialog.handled)
    try:
        i18n.set_language("fr", persist=False)
        assert dialog._skip.text() == "Passer"
        assert dialog._next.text() == "Suivant"
        titles = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "Qu’est-ce qu’OSIA" in titles
        QTest.mouseClick(dialog._skip, Qt.MouseButton.LeftButton)
        state = onboarding.onboarding_state(marker)
        assert state["outcome"] == "skipped"
        assert state["skipped"] is True
        assert [list(item) for item in handled] == [["skipped"]]
    finally:
        i18n.set_language("en", persist=False)


def test_finish_persists_essential_completion_and_second_launch_is_quiet(tmp_path):
    marker = tmp_path / "state.json"
    onboarding.create_fresh_install_marker(marker)
    assert onboarding.claim_automatic_onboarding(marker)
    dialog = OnboardingDialog(automatic=True, state_path=marker)
    dialog._show_lesson("essential.ready", persist=True)
    assert dialog._next.text() == "Finish"
    QTest.mouseClick(dialog._next, Qt.MouseButton.LeftButton)
    state = onboarding.onboarding_state(marker)
    assert state["status"] == "handled"
    assert state["outcome"] == "completed"
    assert state["essentialCompleted"] is True
    assert state["finished"] is True
    assert not onboarding.claim_automatic_onboarding(marker)
    manual = OnboardingDialog(automatic=False, state_path=marker)
    assert manual._current_lesson_id is None
    assert manual._toc.count() == 8
    manual.deleteLater()


def test_close_and_manual_reopen_resume_from_saved_lesson(tmp_path):
    marker = tmp_path / "state.json"
    first = OnboardingDialog(automatic=False, state_path=marker)
    first._show_lesson("advanced.voice-tts-stt", persist=True)
    first.reject()
    state = onboarding.onboarding_state(marker)
    assert state["status"] == "handled"
    assert state["outcome"] == "migrated"
    assert state["currentLesson"] == "advanced.voice-tts-stt"

    reopened = OnboardingDialog(automatic=False, state_path=marker)
    assert reopened._current_lesson_id is None
    QTest.mouseClick(
        reopened._category_buttons[CATEGORY_MASTERY], Qt.MouseButton.LeftButton
    )
    assert reopened._next.text() == "Resume"
    QTest.mouseClick(reopened._next, Qt.MouseButton.LeftButton)
    assert reopened._current_lesson_id == "advanced.voice-tts-stt"
    reopened.deleteLater()


def test_advanced_guide_is_not_forced_on_first_launch(tmp_path):
    marker = tmp_path / "state.json"
    onboarding.create_fresh_install_marker(marker)
    onboarding.claim_automatic_onboarding(marker)
    dialog = OnboardingDialog(automatic=True, state_path=marker)
    dialog.show()
    _QT_APP.processEvents()
    assert dialog._selected_category == CATEGORY_ESSENTIAL
    assert dialog._current_lesson_id == "essential.what-is-osia"
    assert dialog._skip.isVisible()
    dialog.close()


class _DormantThread:
    def __init__(self, *args, target=None, **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)

    def start(self):
        return None


def test_manual_settings_action_reopens_home_without_writing_configuration(monkeypatch):
    monkeypatch.setattr(
        main_popup, "threading", types.SimpleNamespace(Thread=_DormantThread)
    )
    writes = []
    panel = main_popup._SettingsPanel()
    spy = QSignalSpy(panel.getting_started_requested)
    monkeypatch.setattr(main_popup, "set_setting", lambda *args: writes.append(args))
    QTest.mouseClick(panel._guide_btn, Qt.MouseButton.LeftButton)
    assert len(spy) == 1
    assert writes == []
    popup_source = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
    assert "self._settings.getting_started_requested.connect(" in popup_source
    assert "lambda: self.show_onboarding(automatic=False)" in popup_source
    panel.deleteLater()
    _QT_APP.processEvents()


def test_assets_are_curated_bounded_metadata_free_and_packaged():
    referenced = {lesson.asset for lesson in LESSONS if lesson.asset}
    assert referenced == {f"onboarding/{name}" for name in EXPECTED_ASSETS}
    actual = {path.name for path in (ROOT / "assets" / "onboarding").glob("*.png")}
    assert actual == EXPECTED_ASSETS
    for relative in sorted(referenced):
        path = Path(asset_path(relative))
        assert path.is_file()
        assert path.resolve().is_relative_to((ROOT / "assets" / "onboarding").resolve())
        assert path.stat().st_size <= 500_000
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.width <= 900 and image.height <= 580
            assert image.info == {}


def test_assets_and_fixture_copy_have_no_secret_email_or_machine_identity_patterns():
    content = _english_copy()
    generator = (ROOT / "tools" / "generate_onboarding_assets.py").read_text("utf-8")
    combined = content + "\n" + generator
    patterns = (
        r"sk-[A-Za-z0-9_-]{20,}",
        r"ghp_[A-Za-z0-9_]{20,}",
        r"xox[abprs]-[A-Za-z0-9-]{20,}",
        r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    )
    for pattern in patterns:
        assert re.search(pattern, combined) is None
    assert "Aymen" not in combined
    assert "%USERPROFILE%" not in combined
    assert "C:\\Users\\vertu" not in combined


@pytest.mark.parametrize(
    "size", [(1040, 710), (832, 568), (700, 480), (680, 420)]
)
def test_layout_scrolls_wraps_and_has_no_horizontal_overflow(tmp_path, size):
    dialog = OnboardingDialog(state_path=tmp_path / "state.json")
    dialog.resize(*size)
    dialog._show_lesson("advanced.pc-mobile-connection", persist=True)
    dialog.show()
    _QT_APP.processEvents()
    assert dialog._scroll.horizontalScrollBar().maximum() == 0
    assert dialog._scroll.verticalScrollBar().maximum() > 0
    body_labels = [
        label for label in dialog.findChildren(QLabel)
        if label.objectName() in {"guideBody", "guideIntro"}
    ]
    assert body_labels and all(label.wordWrap() for label in body_labels)
    images = dialog.findChildren(_ScaledPixmapLabel)
    assert len(images) == 1
    assert images[0].pixmap() is not None and not images[0].pixmap().isNull()
    dialog.close()


def test_keyboard_focus_shortcuts_and_accessible_image_text(tmp_path):
    dialog = OnboardingDialog(state_path=tmp_path / "state.json")
    dialog._show_lesson("essential.api-key", persist=True)
    for button in dialog.findChildren(QPushButton):
        assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert button.accessibleName().strip()
    assert dialog._toc.accessibleName() == "Contents"
    assert dialog._previous_shortcut.key().toString() == "Alt+Left"
    assert dialog._next_shortcut.key().toString() == "Alt+Right"
    image = dialog.findChild(_ScaledPixmapLabel)
    assert image is not None
    assert "mask" in image.accessibleName().lower()
    assert image.toolTip() == image.accessibleName()
    dialog.deleteLater()
