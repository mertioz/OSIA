"""Emit and compare safe WOR-159 guide manifests without guide body text."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.guide_content import (
    CATEGORIES,
    CATEGORY_ADVANCED,
    CATEGORY_ESSENTIAL,
    CATEGORY_MASTERY,
    GUIDE_CONTENT_SCHEMA_VERSION,
    GuideLesson,
    lessons_for_category,
)


EXPECTED_ESSENTIAL_HASH = "a1da8d997bdbdf0b3bfb8e029db3e92b9126f9e49f7badd5a803b401caf95d20"


def _text(value: dict[str, str] | None) -> dict[str, str] | None:
    return None if value is None else {"en": value["en"], "fr": value["fr"]}


def _essential_record(lesson: GuideLesson) -> dict:
    return {
        "id": lesson.id,
        "order": lesson.order,
        "title": _text(lesson.title),
        "intro": _text(lesson.intro),
        "paragraphs": [_text(value) for value in lesson.paragraphs],
        "exampleTitle": _text(lesson.example_title),
        "example": _text(lesson.example),
        "remember": _text(lesson.remember),
        "asset": lesson.asset,
        "caption": _text(lesson.caption),
        "altText": _text(lesson.alt_text),
    }


def _essential_hash() -> str:
    payload = [_essential_record(item) for item in lessons_for_category(CATEGORY_ESSENTIAL)]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _curriculum(category: str) -> list[dict]:
    return [
        {
            "id": lesson.id,
            "order": lesson.order,
            "subjectId": lesson.subject_id,
            "title": _text(lesson.title),
            "glossaryIds": list(lesson.glossary_ids),
        }
        for lesson in lessons_for_category(category)
    ]


def build_manifest() -> dict:
    return {
        "platform": "pc",
        "schemaVersion": GUIDE_CONTENT_SCHEMA_VERSION,
        "guideIds": [item.id for item in CATEGORIES],
        "categories": [
            {
                "id": item.id,
                "semanticId": item.semantic_id,
                "order": item.order,
                "title": _text(item.title),
            }
            for item in CATEGORIES
        ],
        "essentialHash": _essential_hash(),
        "curriculum": {
            "mastery": _curriculum(CATEGORY_MASTERY),
            "advanced": _curriculum(CATEGORY_ADVANCED),
        },
        "glossaryIds": sorted({
            value
            for category in (CATEGORY_MASTERY, CATEGORY_ADVANCED)
            for lesson in lessons_for_category(category)
            for value in lesson.glossary_ids
        }),
    }


def compare_mobile(pc: dict, mobile: dict) -> None:
    checks = ("schemaVersion", "guideIds", "categories", "curriculum", "glossaryIds")
    mismatched = [key for key in checks if pc.get(key) != mobile.get(key)]
    if mismatched:
        raise SystemExit("guide_manifest_mismatch:" + ",".join(mismatched))
    if mobile.get("platform") != "mobile" or not mobile.get("essentialHash"):
        raise SystemExit("guide_manifest_invalid_mobile_essential_hash")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = build_manifest()
    if EXPECTED_ESSENTIAL_HASH != "TO_BE_RECORDED":
        if manifest["essentialHash"] != EXPECTED_ESSENTIAL_HASH:
            raise SystemExit("pc_essential_content_changed")
    if args.compare:
        compare_mobile(manifest, json.loads(args.compare.read_text(encoding="utf-8")))
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
