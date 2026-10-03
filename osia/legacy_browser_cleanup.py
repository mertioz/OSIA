"""One-time removal of the retired OSIA Chromium messaging registration.

This module is uninstall-only migration support. It does not register a host,
load an extension, or expose browser-session functionality.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from osia.config import PRIVATE_DATA_DIR


_HOST_NAME = "space.osia.browser"
_REGISTRY_PATHS = (
    rf"Software\Google\Chrome\NativeMessagingHosts\{_HOST_NAME}",
    rf"Software\Microsoft\Edge\NativeMessagingHosts\{_HOST_NAME}",
)
_STATE_ROOT = Path(PRIVATE_DATA_DIR) / "personal_browser"
_MANIFEST = _STATE_ROOT / "native_host.json"
_MARKER = _STATE_ROOT / "registration.json"


def remove_legacy_browser_registration() -> dict[str, object]:
    """Remove only the manifest and registry entries proven to be OSIA-owned."""
    if sys.platform != "win32":
        return {"state": "not_applicable", "removed": False}
    try:
        marker = json.loads(_MARKER.read_text("utf-8"))
        owned = (
            marker.get("owner") == _HOST_NAME
            and marker.get("manifest_path") == str(_MANIFEST)
        )
    except (OSError, json.JSONDecodeError):
        owned = False
    if not owned:
        return {"state": "not_registered", "removed": False}

    import winreg

    expected = str(_MANIFEST)
    for key_path in _REGISTRY_PATHS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, kind = winreg.QueryValueEx(key, "")
            if (
                kind == winreg.REG_SZ
                and os.path.normcase(value) == os.path.normcase(expected)
            ):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            continue

    _MANIFEST.unlink(missing_ok=True)
    _MARKER.unlink(missing_ok=True)
    try:
        _STATE_ROOT.rmdir()
    except OSError:
        pass
    return {"state": "not_registered", "removed": True}


__all__ = ["remove_legacy_browser_registration"]
