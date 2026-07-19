#!/usr/bin/env python3
"""Best-effort OS notification helpers for OSIA."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

from osia.utils.subprocess_policy import run_background_probe

_SECRET_HINT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|bearer)\b|"
    r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}\b"
)


def safe_task_title(value: object, *, fallback: str = "Scheduled task") -> str:
    """Return a short one-line task title safe enough for notifications."""

    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or _SECRET_HINT_RE.search(text):
        return fallback
    return text[:80]


def _safe_line(value: object, *, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if _SECRET_HINT_RE.search(text):
        text = "Scheduled task"
    return text[:limit]


def _notify_linux(title: str, body: str) -> bool:
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return False
    try:
        subprocess.run(
            [notify_send, title, body],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except Exception:
        return False


def _notify_windows(title: str, body: str) -> bool:
    try:
        from winotify import Notification

        Notification(app_id="OSIA", title=title, msg=body).show()
        return True
    except Exception:
        pass
    try:
        from win10toast import ToastNotifier

        ToastNotifier().show_toast(title, body, duration=5, threaded=True)
        return True
    except Exception:
        pass
    try:
        from plyer import notification

        notification.notify(title=title, message=body, app_name="OSIA", timeout=5)
        return True
    except Exception:
        pass

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return False
    script = (
        "if (Get-Module -ListAvailable -Name BurntToast) { "
        "Import-Module BurntToast; "
        "New-BurntToastNotification -Text @($args[0], $args[1]); exit 0 "
        "} exit 1"
    )
    try:
        result = run_background_probe(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
                title,
                body,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        return result.returncode == 0
    except Exception:
        return False


def notify(title: object, body: object) -> bool:
    """Show a short system notification when the platform supports one."""

    safe_title = _safe_line(title, limit=80) or "OSIA"
    safe_body = _safe_line(body, limit=120)
    if not safe_body:
        return False
    if sys.platform.startswith("linux"):
        return _notify_linux(safe_title, safe_body)
    if sys.platform == "win32":
        return _notify_windows(safe_title, safe_body)
    return False
