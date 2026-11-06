"""Authoritative OSIA product version.

Protocol and data-format versions intentionally live beside their respective
protocols.  Only the installable product version belongs here.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


PRODUCT_VERSION = "0.9.248"
VERSION = PRODUCT_VERSION
RELEASE_SEQUENCE = 2026110601
FIRST_RELEASE_SEQUENCE = 2026102001

# Display aliases only. Original identifiers remain the artifact/diagnostic keys.
HISTORICAL_DISPLAY_VERSIONS = {
    "1.0.0": "0.8.0", "1.0.1": "0.8.1", "1.0.2": "0.8.2",
    "1.0.3": "0.8.3", "1.0.4": "0.8.4", "1.0.5": "0.8.5",
    "1.0.6": "0.8.6", "1.1.0": "0.8.944", "1.1.1": "0.8.945",
    "1.1.2": "0.8.946", "1.1.3": "0.8.947", "1.1.4": "0.8.948",
    "1.1.5": "0.8.949", "1.1.6": "0.8.950", "1.1.7": "0.8.951",
    "1.1.8": "0.8.952",
}


def display_version(version: str) -> str:
    if version == "0.9.248":
        return "0.9.0248"
    if version == "0.9.1":
        return "0.9.01"
    return HISTORICAL_DISPLAY_VERSIONS.get(version, version)


def display_historical_text(text: str) -> str:
    """Relabel prose only; preserve embedded artifact URLs exactly."""
    mobile_families = {
        "1.0.0": "0.8.0", "1.0.1": "0.8.1–0.8.2", "1.0.2": "0.8.3",
        "1.1.0": "0.8.4", "1.1.1": "0.8.5–0.8.6", "1.1.2": "0.8.7–0.8.10",
    }
    def label(match: re.Match) -> str:
        if match.group("mobile"):
            original = match.group("mobile_version")
            return match.group("mobile") + mobile_families.get(original, original)
        return display_version(match.group())
    return re.sub(
        r"https?://\S+|(?P<mobile>(?:Android|iOS|Mobile)\s+)(?P<mobile_version>1\.[01]\.\d+)(?![\w.])|(?<![\w.])(?:1\.0\.[0-6]|1\.1\.[0-8])(?![\w.])",
        label, text, flags=re.IGNORECASE,
    )


def legacy_release_sequence(version: str) -> int:
    """Known pre-transition identities only; never infer a future 1.x epoch."""
    if version not in HISTORICAL_DISPLAY_VERSIONS:
        raise ValueError("unknown_legacy_release")
    major, minor, patch = parse_product_version(version)
    return major * 10000 + minor * 100 + patch

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
    "RELEASE_SEQUENCE", "HISTORICAL_DISPLAY_VERSIONS", "display_version",
    "display_historical_text", "legacy_release_sequence",
]
