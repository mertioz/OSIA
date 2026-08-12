"""Per-user Windows Native Messaging registration owned exactly by OSIA."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from osia.config import PRIVATE_DATA_DIR


HOST_NAME = "space.osia.browser"
_REGISTRY_PATHS = {
    "chrome": rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    "edge": rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
}
_ROOT = Path(__file__).resolve().parents[2]
_STATE_ROOT = Path(PRIVATE_DATA_DIR) / "personal_browser"
_MANIFEST = _STATE_ROOT / "native_host.json"
_MARKER = _STATE_ROOT / "registration.json"


def current_user_sid() -> str:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(sid)
    finally:
        token.Close()


def pipe_name() -> str:
    suffix = hashlib.sha256(current_user_sid().encode("ascii")).hexdigest()[:24]
    return rf"\\.\pipe\space.osia.browser.{suffix}"


def _host_executable() -> Path | None:
    configured = os.environ.get("OSIA_NATIVE_HOST_PATH", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "native_host" / "OSIA-NativeHost.exe")
    candidates.extend((
        _ROOT / "dist" / "native_host" / "OSIA-NativeHost" / "OSIA-NativeHost.exe",
        _ROOT / "dist" / "native_host" / "OSIA-NativeHost.exe",
    ))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve(strict=False)
        if resolved.is_file() and resolved.suffix.lower() == ".exe":
            return resolved
    return None


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")
    os.replace(temporary, path)


def setup(*, allowed_origins: list[str], development: bool = False) -> dict:
    import winreg

    host = _host_executable()
    if host is None:
        return {"state": "host_missing", "registered": False}
    if not allowed_origins:
        return {"state": "setup_required", "reason": "production_store_ids_missing", "registered": False}
    manifest = {
        "name": HOST_NAME,
        "description": "OSIA Personal Browser local bridge",
        "path": str(host),
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }
    _write_private_json(_MANIFEST, manifest)
    registered = []
    try:
        _write_private_json(_MARKER, {
            "schema_version": 1,
            "owner": HOST_NAME,
            "development": bool(development),
            "host_path": str(host),
            "manifest_path": str(_MANIFEST),
            "registry_families": [],
        })
        for family, key_path in _REGISTRY_PATHS.items():
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(_MANIFEST))
            registered.append(family)
        _write_private_json(_MARKER, {
            "schema_version": 1,
            "owner": HOST_NAME,
            "development": bool(development),
            "host_path": str(host),
            "manifest_path": str(_MANIFEST),
            "registry_families": registered,
        })
    except OSError:
        remove()
        return {"state": "setup_required", "reason": "registration_failed", "registered": False}
    return {
        "state": "no_authorized_tab",
        "registered": True,
        "families": registered,
        "development": bool(development),
    }


def registration_status() -> dict:
    import winreg

    if not _MARKER.is_file() or not _MANIFEST.is_file():
        return {"state": "setup_required", "registered": False}
    try:
        marker = json.loads(_MARKER.read_text("utf-8"))
        manifest = json.loads(_MANIFEST.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "setup_required", "registered": False}
    if marker.get("owner") != HOST_NAME or manifest.get("name") != HOST_NAME:
        return {"state": "setup_required", "registered": False}
    expected = str(_MANIFEST)
    families = []
    for family, key_path in _REGISTRY_PATHS.items():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, kind = winreg.QueryValueEx(key, "")
            if kind == winreg.REG_SZ and os.path.normcase(value) == os.path.normcase(expected):
                families.append(family)
        except OSError:
            continue
    host = Path(str(manifest.get("path") or ""))
    if not host.is_file():
        return {"state": "host_missing", "registered": bool(families), "families": families}
    return {
        "state": "no_authorized_tab",
        "registered": len(families) == len(_REGISTRY_PATHS),
        "families": families,
        "development": bool(marker.get("development")),
    }


def registered_allowed_origins() -> list[str]:
    try:
        marker = json.loads(_MARKER.read_text("utf-8"))
        manifest = json.loads(_MANIFEST.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if marker.get("owner") != HOST_NAME or marker.get("manifest_path") != str(_MANIFEST):
        return []
    values = manifest.get("allowed_origins")
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if isinstance(item, str)]


def remove() -> dict:
    import winreg

    owned = False
    try:
        marker = json.loads(_MARKER.read_text("utf-8"))
        owned = marker.get("owner") == HOST_NAME and marker.get("manifest_path") == str(_MANIFEST)
    except (OSError, json.JSONDecodeError):
        pass
    if not owned:
        return {"state": "setup_required", "removed": False}
    for key_path in _REGISTRY_PATHS.values():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, kind = winreg.QueryValueEx(key, "")
            if kind == winreg.REG_SZ and os.path.normcase(value) == os.path.normcase(str(_MANIFEST)):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            continue
    _MANIFEST.unlink(missing_ok=True)
    _MARKER.unlink(missing_ok=True)
    return {"state": "setup_required", "removed": True}


def repair_owned_registration() -> dict:
    origins = registered_allowed_origins()
    if not origins:
        return {"state": "setup_required", "repaired": False}
    try:
        marker = json.loads(_MARKER.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "setup_required", "repaired": False}
    result = setup(
        allowed_origins=origins,
        development=bool(marker.get("development")),
    )
    return dict(result, repaired=result.get("registered") is True)


__all__ = ["HOST_NAME", "current_user_sid", "pipe_name", "registered_allowed_origins", "registration_status", "remove", "repair_owned_registration", "setup"]
