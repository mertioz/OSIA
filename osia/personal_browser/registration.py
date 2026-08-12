"""Cross-platform Personal Browser setup facade and extension identity policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


DEVELOPMENT_EXTENSION_ID = "nodfobpjbbeikjngnfabmnfnklmcmglj"
_ROOT = Path(__file__).resolve().parents[2]


def _metadata() -> dict:
    candidates = [
        Path(sys.executable).resolve().parent / "_internal" / "personal_browser" / "extension_metadata.json",
        _ROOT / "build" / "windows" / "generated" / "personal_browser_metadata.json",
        _ROOT / "extension" / "personal-browser" / "metadata.json",
    ]
    for path in candidates:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def allowed_origins(*, development: bool | None = None) -> list[str]:
    dev = not getattr(sys, "frozen", False) if development is None else bool(development)
    if dev:
        return [f"chrome-extension://{DEVELOPMENT_EXTENSION_ID}/"]
    metadata = _metadata()
    ids = [
        str(metadata.get("chrome_store_id") or "").strip(),
        str(metadata.get("edge_store_id") or "").strip(),
    ]
    if any(not value or len(value) != 32 for value in ids):
        return []
    return [f"chrome-extension://{value}/" for value in ids]


def origin_allowed(origin: str, *, development: bool | None = None) -> bool:
    candidate = str(origin or "")
    if sys.platform == "win32":
        try:
            from osia.personal_browser.registration_windows import registered_allowed_origins

            registered = registered_allowed_origins()
            if registered:
                return candidate in registered
        except Exception:
            pass
    return candidate in allowed_origins(development=development)


def setup_personal_browser(*, development: bool | None = None) -> dict:
    dev = not getattr(sys, "frozen", False) if development is None else bool(development)
    if sys.platform == "win32":
        from osia.personal_browser.registration_windows import setup

        result = setup(allowed_origins=allowed_origins(development=dev), development=dev)
    else:
        from osia.personal_browser.registration_posix import setup

        result = setup(development=dev)
    if result.get("registered"):
        from osia.config import set_setting

        set_setting("personal_browser_enabled", True)
    return result


def registration_status() -> dict:
    if sys.platform == "win32":
        from osia.personal_browser.registration_windows import registration_status as status
    else:
        from osia.personal_browser.registration_posix import registration_status as status
    return status()


def remove_personal_browser() -> dict:
    if sys.platform == "win32":
        from osia.personal_browser.registration_windows import remove
    else:
        from osia.personal_browser.registration_posix import remove
    result = remove()
    if result.get("removed"):
        from osia.config import set_setting

        set_setting("personal_browser_enabled", False)
        set_setting("personal_browser_instance_hint", "")
    return result


def development_extension_path() -> Path:
    return _ROOT / "extension" / "personal-browser"


__all__ = [
    "DEVELOPMENT_EXTENSION_ID", "allowed_origins", "development_extension_path",
    "origin_allowed", "registration_status", "remove_personal_browser",
    "setup_personal_browser",
]
