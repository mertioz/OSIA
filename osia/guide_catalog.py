"""Typed offline loader for the canonical semantic OSIA Guide catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from osia.utils.assets import asset_path


CATALOG_SCHEMA_VERSION = 4
CATALOG_ASSET = "guide/catalog.v4.json"
TRACK_ESSENTIAL = "getting-started"
TRACK_MASTERY = "master-osia"
TRACK_ADVANCED = "osia-advanced"

Localized = dict[str, str]


def localized(value: Localized | None, language: str) -> str:
    """Resolve Guide copy with the established English fallback."""

    if not value:
        return ""
    return value.get(language) or value.get("en") or ""


@dataclass(frozen=True)
class GuideCallout:
    kind: str
    title: Localized
    body: Localized


@dataclass(frozen=True)
class GuideVisual:
    kind: str
    key: str
    alt: Localized
    asset: str | None = None


@dataclass(frozen=True)
class GuideSlide:
    id: str
    track_id: str
    order: int
    title: Localized
    subtitle: Localized
    body_blocks: tuple[Localized, ...]
    callouts: tuple[GuideCallout, ...]
    visual: GuideVisual
    legacy_ids: tuple[str, ...]

    @property
    def category(self) -> str:
        """Compatibility name used by the former lesson renderer."""

        return self.track_id


@dataclass(frozen=True)
class GuideTrack:
    id: str
    semantic_id: str
    order: int
    title: Localized
    summary: Localized
    slides: tuple[GuideSlide, ...]


@dataclass(frozen=True)
class GuideCatalog:
    schema_version: int
    content_version: int
    tracks: tuple[GuideTrack, ...]

    @property
    def slides(self) -> tuple[GuideSlide, ...]:
        return tuple(slide for track in self.tracks for slide in track.slides)


def _localized(value: object, field: str) -> Localized:
    if not isinstance(value, dict):
        raise ValueError(f"guide_{field}_not_object")
    result = {language: value.get(language) for language in ("en", "fr")}
    if not all(isinstance(text, str) and text.strip() for text in result.values()):
        raise ValueError(f"guide_{field}_incomplete")
    return result  # type: ignore[return-value]


def _resolve_variant(raw: dict, platform: str) -> dict:
    value = dict(raw)
    variants = value.pop("platform_variants", {})
    if isinstance(variants, dict):
        variant = variants.get(platform)
        if isinstance(variant, dict):
            value.update(variant)
    return value


def _parse_slide(raw: object, track_id: str, platform: str) -> GuideSlide:
    if not isinstance(raw, dict):
        raise ValueError("guide_slide_not_object")
    value = _resolve_variant(raw, platform)
    if value.get("track_id") != track_id:
        raise ValueError("guide_slide_track")
    visual_raw = value.get("visual")
    if not isinstance(visual_raw, dict):
        raise ValueError("guide_visual_not_object")
    callouts_raw = value.get("callouts")
    if not isinstance(callouts_raw, list):
        raise ValueError("guide_callouts_not_array")
    body_raw = value.get("body_blocks")
    if not isinstance(body_raw, list):
        raise ValueError("guide_body_not_array")
    legacy_raw = value.get("legacy_ids")
    if not isinstance(legacy_raw, list) or not all(isinstance(item, str) for item in legacy_raw):
        raise ValueError("guide_legacy_ids")
    return GuideSlide(
        id=str(value["slide_id"]),
        track_id=track_id,
        order=int(value["order"]),
        title=_localized(value.get("title"), "title"),
        subtitle=_localized(value.get("subtitle"), "subtitle"),
        body_blocks=tuple(_localized(item, "body") for item in body_raw),
        callouts=tuple(
            GuideCallout(
                kind=str(item["kind"]),
                title=_localized(item.get("title"), "callout_title"),
                body=_localized(item.get("body"), "callout_body"),
            )
            for item in callouts_raw
            if isinstance(item, dict)
        ),
        visual=GuideVisual(
            kind=str(visual_raw["kind"]),
            key=str(visual_raw["key"]),
            alt=_localized(visual_raw.get("alt"), "visual_alt"),
            asset=str(visual_raw["asset"]) if visual_raw.get("asset") else None,
        ),
        legacy_ids=tuple(legacy_raw),
    )


@lru_cache(maxsize=2)
def load_catalog(platform: str = "pc") -> GuideCatalog:
    """Load and validate the packaged catalog for ``pc`` or ``mobile``."""

    if platform not in {"pc", "mobile"}:
        raise ValueError("guide_platform")
    path = Path(asset_path(CATALOG_ASSET))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("guide_catalog_unavailable") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("guide_schema_version")
    tracks_raw = raw.get("tracks")
    if not isinstance(tracks_raw, list):
        raise ValueError("guide_tracks_not_array")
    tracks: list[GuideTrack] = []
    ids: set[str] = set()
    for expected_track_order, track_raw in enumerate(tracks_raw, 1):
        if not isinstance(track_raw, dict):
            raise ValueError("guide_track_not_object")
        track_id = str(track_raw["track_id"])
        slides_raw = track_raw.get("slides")
        if not isinstance(slides_raw, list):
            raise ValueError("guide_slides_not_array")
        slides = tuple(
            _parse_slide(item, track_id, platform) for item in slides_raw
        )
        if [slide.order for slide in slides] != list(range(1, len(slides) + 1)):
            raise ValueError("guide_slide_order")
        for slide in slides:
            if slide.id in ids:
                raise ValueError("guide_duplicate_slide")
            ids.add(slide.id)
        track = GuideTrack(
            id=track_id,
            semantic_id=str(track_raw["semantic_id"]),
            order=int(track_raw["order"]),
            title=_localized(track_raw.get("title"), "track_title"),
            summary=_localized(track_raw.get("summary"), "track_summary"),
            slides=slides,
        )
        if track.order != expected_track_order:
            raise ValueError("guide_track_order")
        tracks.append(track)
    if [track.semantic_id for track in tracks] != ["essential", "mastery", "advanced"]:
        raise ValueError("guide_track_semantics")
    return GuideCatalog(
        schema_version=CATALOG_SCHEMA_VERSION,
        content_version=int(raw.get("content_version", 0)),
        tracks=tuple(tracks),
    )


def tracks(platform: str = "pc") -> tuple[GuideTrack, ...]:
    return load_catalog(platform).tracks


def slides_for_track(track_id: str, platform: str = "pc") -> tuple[GuideSlide, ...]:
    for track in tracks(platform):
        if track.id == track_id:
            return track.slides
    return ()


def slide_by_id(slide_id: str | None, platform: str = "pc") -> GuideSlide | None:
    if not slide_id:
        return None
    for slide in load_catalog(platform).slides:
        if slide.id == slide_id:
            return slide
    return None


def alias_map(platform: str = "pc") -> dict[str, str]:
    result: dict[str, str] = {}
    for slide in load_catalog(platform).slides:
        for alias in slide.legacy_ids:
            result.setdefault(alias, slide.id)
    return result


def resolve_slide_id(
    value: str | None,
    *,
    track_id: str | None = None,
    old_order: int | None = None,
    platform: str = "pc",
) -> str | None:
    """Resolve current/legacy progress with deterministic removed-slide fallback."""

    current = slide_by_id(value, platform)
    if current is not None:
        return current.id
    if value:
        alias = alias_map(platform).get(value)
        if alias:
            return alias
    candidates = slides_for_track(track_id or TRACK_ESSENTIAL, platform)
    if not candidates:
        return None
    if isinstance(old_order, int):
        for slide in candidates:
            if slide.order >= old_order:
                return slide.id
        for slide in reversed(candidates):
            if slide.order < old_order:
                return slide.id
    return candidates[0].id


def legacy_children(legacy_id: str, platform: str = "pc") -> tuple[str, ...]:
    """Return all children produced from one legacy lesson in stable order."""

    children = [
        slide.id
        for slide in load_catalog(platform).slides
        if legacy_id in slide.legacy_ids or slide.id == legacy_id
    ]
    return tuple(dict.fromkeys(children))


__all__ = [
    "CATALOG_ASSET",
    "CATALOG_SCHEMA_VERSION",
    "GuideCallout",
    "GuideCatalog",
    "GuideSlide",
    "GuideTrack",
    "GuideVisual",
    "TRACK_ADVANCED",
    "TRACK_ESSENTIAL",
    "TRACK_MASTERY",
    "alias_map",
    "legacy_children",
    "load_catalog",
    "localized",
    "resolve_slide_id",
    "slide_by_id",
    "slides_for_track",
    "tracks",
]
