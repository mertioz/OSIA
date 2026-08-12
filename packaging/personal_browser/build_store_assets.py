"""Generate deterministic Personal Browser extension and store imagery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ICON = ROOT / "assets" / "osia_icon.png"
EXTENSION_ICONS = ROOT / "extension" / "personal-browser" / "icons"
STORE_ASSETS = Path(__file__).resolve().parent / "store_submission" / "assets"
PNG_OPTIONS = {"compress_level": 9, "optimize": False}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asset_record(path: Path) -> dict:
    with Image.open(path) as image:
        width, height = image.size
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "width": width,
        "height": height,
        "sha256": _sha256(path),
    }


def _mark(source: Image.Image, size: tuple[int, int], occupancy: float) -> Image.Image:
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    mark = source.copy()
    maximum = (
        max(1, round(size[0] * occupancy)),
        max(1, round(size[1] * occupancy)),
    )
    mark.thumbnail(maximum, Image.Resampling.LANCZOS)
    canvas.alpha_composite(
        mark,
        ((size[0] - mark.width) // 2, (size[1] - mark.height) // 2),
    )
    return canvas


def _promo(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    width, height = size
    background = Image.new("RGBA", size, (15, 9, 22, 255))
    draw = ImageDraw.Draw(background)
    for y in range(height):
        mix = y / max(1, height - 1)
        draw.line(
            (0, y, width, y),
            fill=(
                15 + round(18 * mix),
                9 + round(8 * mix),
                22 + round(27 * mix),
                255,
            ),
        )

    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    radius = round(min(width, height) * 0.64)
    center = (width // 2, height // 2)
    glow_draw.ellipse(
        (
            center[0] - radius,
            center[1] - radius,
            center[0] + radius,
            center[1] + radius,
        ),
        fill=(124, 58, 237, 120),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(max(8, round(radius * 0.32))))
    background = Image.alpha_composite(background, glow)

    logo = _mark(source, size, 0.48 if width / height < 2 else 0.38)
    return Image.alpha_composite(background, logo).convert("RGB")


def build() -> dict:
    EXTENSION_ICONS.mkdir(parents=True, exist_ok=True)
    STORE_ASSETS.mkdir(parents=True, exist_ok=True)
    with Image.open(SOURCE_ICON) as opened:
        source = opened.convert("RGBA")

    outputs: list[Path] = []
    for size in (16, 32, 48, 128):
        target = EXTENSION_ICONS / f"icon{size}.png"
        _mark(source, (size, size), 0.875).save(target, **PNG_OPTIONS)
        outputs.append(target)

    edge_logo = STORE_ASSETS / "edge-logo-300.png"
    _mark(source, (300, 300), 0.86).save(edge_logo, **PNG_OPTIONS)
    outputs.append(edge_logo)

    small = STORE_ASSETS / "promo-small-440x280.png"
    _promo(source, (440, 280)).save(small, **PNG_OPTIONS)
    outputs.append(small)

    marquee = STORE_ASSETS / "promo-marquee-1400x560.png"
    _promo(source, (1400, 560)).save(marquee, **PNG_OPTIONS)
    outputs.append(marquee)

    manifest = {
        "schema_version": 1,
        "source": {
            "path": SOURCE_ICON.relative_to(ROOT).as_posix(),
            "sha256": _sha256(SOURCE_ICON),
        },
        "assets": [_asset_record(path) for path in outputs],
    }
    target = STORE_ASSETS / "assets-manifest.json"
    target.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    print(json.dumps(build(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
