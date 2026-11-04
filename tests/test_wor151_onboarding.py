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
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

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
from osia.gui.onboarding import OnboardingDialog
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
    assert progress["currentLesson"] == "mastery.voice-tts"
    assert progress["masteryCompleted"] == [
        "mastery.mobile-pairing",
        "mastery.mobile-transport",
        "mastery.single-transport",
        "mastery.mobile-offline",
    ]
    assert progress["advancedCompleted"] == []
    onboarding.update_guide_progress(marker, finished=True)
    state = onboarding.onboarding_state(marker)
    assert state["outcome"] == "completed"
    assert state["currentLesson"] == "mastery.voice-tts"
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
    assert progress["currentLesson"] == "essential.osia-role"
    assert progress["masteryCompleted"] == [
        "mastery.voice-tts",
        "mastery.voice-stt",
        "mastery.voice-recovery",
        "mastery.voice-privacy",
    ]
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
    assert progress["currentLesson"] == "mastery.manual-model"
    assert progress["masteryCompleted"] == [
        "mastery.manual-model",
        "mastery.task-routing",
        "mastery.model-fallback",
        "mastery.vision-local",
    ]
    assert progress["advancedCompleted"] == []
    assert progress["finished"] is True
    assert onboarding.onboarding_state(marker)["outcome"] == "completed"


def test_installer_marks_only_fresh_non_update_profiles():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    batch = (ROOT / "install.bat").read_text("utf-8")
    assert "FreshInstallOnboardingEligible" in script
    assert "OsiaProfileDirectory := ResolveOsiaProfileDirectory" in script
    assert "not DirExists(OsiaProfileDirectory)" in script
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
    assert "onboarding.SLIDE_COUNT != 14" in launcher
    assert '"guideCaption"' in launcher
    assert "guide asset" in launcher
    assert '(str(ROOT / "assets"), "assets")' in spec


@pytest.mark.parametrize("language", ["en", "fr"])
def test_settings_guide_has_fourteen_illustrated_steps_and_one_arrow(tmp_path, language):
    marker = tmp_path / "onboarding.json"
    i18n.set_language(language, persist=False)
    dialog = OnboardingDialog(state_path=marker)
    try:
        dialog.resize(dialog.minimumSize())
        dialog.show()
        _QT_APP.processEvents()
        assert dialog.findChildren(QPushButton) == [dialog._next]
        assert dialog._next.text() == "→"
        assert dialog._next.shortcut().toString() == "Right"
        assert dialog._next.isFlat()
        assert dialog.SLIDE_COUNT == 14
        assert dialog._illustration.geometry().bottom() < dialog._caption.geometry().top()
        captions = []
        frames = []
        for _ in range(dialog.SLIDE_COUNT):
            caption = dialog._caption.text()
            assert 1 <= caption.count(".") <= 4
            assert dialog._caption.heightForWidth(dialog._caption.width()) <= dialog._caption.height()
            captions.append(dialog._caption.text())
            frames.append(dialog._illustration.grab().toImage())
            dialog._next.click()
            _QT_APP.processEvents()
        assert len(set(captions)) == 14
        assert all(frame != frames[0] for frame in frames[1:])
        assert not dialog.isVisible()
        assert not marker.exists()
    finally:
        dialog.close()
        dialog.deleteLater()
        i18n.set_language("en", persist=False)


@pytest.mark.parametrize("finish", [True, False])
def test_settings_guide_preserves_first_install_handling(tmp_path, finish):
    marker = tmp_path / "onboarding.json"
    assert onboarding.create_fresh_install_marker(marker)
    assert onboarding.claim_automatic_onboarding(marker)
    dialog = OnboardingDialog(automatic=True, state_path=marker)
    handled = QSignalSpy(dialog.handled)
    try:
        dialog.show()
        if finish:
            for _ in range(dialog.SLIDE_COUNT):
                dialog._next.click()
        else:
            dialog.close()
        outcome = "completed" if finish else "closed"
        assert [list(item) for item in handled] == [[outcome]]
        assert onboarding.onboarding_state(marker)["outcome"] == outcome
        assert not onboarding.claim_automatic_onboarding(marker)
    finally:
        dialog.close()
        dialog.deleteLater()


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


def test_mastery_and_developer_progress_are_independent(tmp_path):
    marker = tmp_path / "state.json"
    progress = onboarding.update_guide_progress(
        marker,
        mastery_completed=["advanced.ai-model-hierarchy"],
        advanced_completed=["advanced.handshake"],
    )
    assert progress["masteryCompleted"] == [
        "mastery.manual-model",
        "mastery.task-routing",
        "mastery.model-fallback",
        "mastery.vision-local",
    ]
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
