from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from osia.guide_catalog import (
    TRACK_ADVANCED,
    TRACK_ESSENTIAL,
    TRACK_MASTERY,
    legacy_children,
    load_catalog,
    localized,
    resolve_slide_id,
    slide_by_id,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "assets" / "guide" / "catalog.v4.json"
SCHEMA_PATH = ROOT / "tools" / "guide_catalog.schema.json"
MOBILE_ROOT = ROOT.parent / "osia-mobile"


def _catalog_text(slide, language: str) -> str:
    parts = [localized(slide.subtitle, language)]
    parts.extend(localized(block, language) for block in slide.body_blocks)
    for callout in slide.callouts:
        parts.extend((localized(callout.title, language), localized(callout.body, language)))
    return " ".join(parts)


def test_catalog_matches_schema_order_localization_and_accessibility_contract():
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(raw)

    catalog = load_catalog()
    assert catalog.schema_version == catalog.content_version == 4
    assert [track.id for track in catalog.tracks] == [
        TRACK_ESSENTIAL,
        TRACK_MASTERY,
        TRACK_ADVANCED,
    ]
    assert [track.semantic_id for track in catalog.tracks] == [
        "essential",
        "mastery",
        "advanced",
    ]
    assert [len(track.slides) for track in catalog.tracks] == [17, 46, 43]
    assert len({slide.id for slide in catalog.slides}) == 106

    for track in catalog.tracks:
        assert [slide.order for slide in track.slides] == list(
            range(1, len(track.slides) + 1)
        )
        assert localized(track.title, "de") == localized(track.title, "en")
        for slide in track.slides:
            assert localized(slide.title, "en")
            assert localized(slide.title, "fr")
            assert localized(slide.title, "de") == localized(slide.title, "en")
            assert localized(slide.visual.alt, "en")
            assert localized(slide.visual.alt, "fr")


def test_catalog_preserves_audited_source_copy_and_only_corrects_settings_facts():
    from osia.guide_content import LESSONS
    from tools.build_guide_catalog import _sentences

    settings_ids = {"essential.menu-tour", "mastery.settings-tour"}
    for lesson in LESSONS:
        children = [slide_by_id(value) for value in legacy_children(lesson.id)]
        assert children and all(children)
        if lesson.id in settings_ids:
            continue
        for language in ("en", "fr"):
            corpus = " ".join(_catalog_text(slide, language) for slide in children)
            source_parts = [lesson.intro, *lesson.paragraphs]
            for source in source_parts:
                for sentence in _sentences(source[language]):
                    assert sentence.replace("\n", " ") in corpus.replace("\n", " ")
            assert lesson.example_title[language] in corpus
            assert lesson.example[language].replace("\n", " ") in corpus.replace("\n", " ")
            if lesson.remember:
                assert lesson.remember[language] in corpus

    settings = " ".join(
        _catalog_text(slide, "en")
        for slide in load_catalog().slides
        if "settings" in slide.id or slide.id == "essential.pc-settings-map"
    )
    assert "eleven real categories" in settings
    assert "Permissions" in settings
    assert "ten real categories" not in settings
    assert not any(
        token in slide.id
        for slide in load_catalog().slides
        for token in ("projects", "reflection", "multi-action")
    )


def test_platform_variants_assets_and_legacy_migrations_are_deterministic():
    pc = slide_by_id("essential.pairing-token", "pc")
    mobile = slide_by_id("essential.pairing-token", "mobile")
    assert pc is not None and mobile is not None
    assert pc.visual.asset == "onboarding/settings-mobile-pairing.png"
    assert mobile.visual.asset == "assets/guide/pc-pairing.png"
    assert [slide.visual.kind for slide in load_catalog().slides].count("screenshot") == 1

    assert resolve_slide_id("advanced.voice-tts-stt") == "mastery.voice-tts"
    assert legacy_children("advanced.voice-tts-stt") == (
        "mastery.voice-tts",
        "mastery.voice-stt",
        "mastery.voice-recovery",
        "mastery.voice-privacy",
    )
    assert resolve_slide_id("removed", track_id=TRACK_MASTERY, old_order=8) == (
        "mastery.manual-model"
    )

    result = subprocess.run(
        [sys.executable, "tools/build_guide_catalog.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "counts=17,46,43" in result.stdout

    mobile_catalog = MOBILE_ROOT / "assets" / "guide" / "catalog.v4.json"
    if mobile_catalog.is_file():
        assert mobile_catalog.read_bytes() == CATALOG_PATH.read_bytes()
        digest = hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()
        assert f"guide_catalog_sha256={digest}" in result.stdout
