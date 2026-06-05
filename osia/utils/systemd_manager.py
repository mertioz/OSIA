"""Thin wrapper around `systemctl` for OSIA's GUI toggle.

Privileged operations (enable/disable) are routed through `pkexec` so they
work from a desktop session without a controlling terminal. Read-only
queries (`is-enabled`, `is-active`) don't need elevation.
"""

import subprocess


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def is_service_enabled(name: str) -> bool:
    """True if the unit is enabled (will start at boot)."""
    try:
        result = _run(["systemctl", "is-enabled", name])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.stdout.strip() == "enabled"


def get_service_status(name: str) -> str:
    """Return one of 'running', 'stopped', 'error'."""
    try:
        result = _run(["systemctl", "is-active", name])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "error"
    state = result.stdout.strip()
    if state == "active":
        return "running"
    if state in ("inactive", "deactivating", "activating"):
        return "stopped"
    return "error"


def enable_service(name: str) -> bool:
    """Enable and start the unit. Returns True on success."""
    try:
        result = _run(
            ["pkexec", "systemctl", "enable", "--now", name],
            timeout=60.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


def disable_service(name: str) -> bool:
    """Disable and stop the unit. Returns True on success."""
    try:
        result = _run(
            ["pkexec", "systemctl", "disable", "--now", name],
            timeout=60.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0


def restart_service(name: str) -> bool:
    """Restart the unit (without changing its enabled state). Returns True on
    success."""
    try:
        result = _run(
            ["pkexec", "systemctl", "restart", name],
            timeout=60.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0
