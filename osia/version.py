"""Authoritative OSIA product version.

Protocol and data-format versions intentionally live beside their respective
protocols.  Only the installable product version belongs here.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


PRODUCT_VERSION = "1.0.2"
VERSION = PRODUCT_VERSION

_PRODUCT_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})\.(?:0|[1-9][0-9]{0,4})"
)


def parse_product_version(value: str) -> tuple[int, int, int]:
    """Parse the supported ``MAJOR.MINOR.PATCH`` product-version format."""
    if not isinstance(value, str) or _PRODUCT_VERSION_RE.fullmatch(value) is None:
        raise ValueError("invalid_product_version")
    parts = tuple(int(part) for part in value.split("."))
    if any(part > 65535 for part in parts):
        raise ValueError("invalid_product_version")
    return parts


parse_product_version(PRODUCT_VERSION)


def current_product_version() -> str:
    """Return the embedded build version, or the source version in source mode."""
    if not getattr(sys, "frozen", False):
        return PRODUCT_VERSION
    root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    try:
        payload = json.loads((root / "app_metadata.json").read_text("utf-8"))
        version = payload["version"]
        parse_product_version(version)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("frozen OSIA app metadata is invalid") from exc
    return version


__all__ = [
    "PRODUCT_VERSION", "VERSION", "current_product_version", "parse_product_version",
]
