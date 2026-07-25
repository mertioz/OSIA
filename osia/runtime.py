"""Runtime paths shared by source and frozen OSIA builds.

The installed Windows application is a PyInstaller ``onedir`` bundle.  Runtime
resources therefore live below PyInstaller's extraction/bundle root, while all
user data continues to be resolved by :mod:`osia.config`.
"""

from __future__ import annotations

import sys
from pathlib import Path


SERVICE_MODE = "--osia-service"
SERVER_MODE = "--osia-server"
WAKE_RECOVERY_MODE = "--osia-wake-recovery"
INSTALL_INTEGRATION_MODE = "--osia-install-integration"
REMOVE_INTEGRATION_MODE = "--osia-remove-integration"
PACKAGE_SMOKE_MODE = "--osia-package-smoke"
RELAUNCH_MODE = "--osia-relaunch"
DOCUMENT_WORKER_MODE = "--osia-document-worker"


def is_frozen() -> bool:
    """Return whether this process is running from a frozen application."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Root containing read-only resources shipped with OSIA."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def application_executable() -> Path:
    """Executable used to launch another OSIA mode."""
    return Path(sys.executable).resolve()


def application_dir() -> Path:
    """Installed application directory, or the repository root in source."""
    if is_frozen():
        return application_executable().parent
    return resource_root()


def resource_path(*parts: str) -> Path:
    """Resolve a shipped, read-only resource without consulting the CWD."""
    return resource_root().joinpath(*parts)


def bundled_model_path(name: str) -> Path | None:
    """Return a bundled model directory when present."""
    candidate = resource_path("models", name)
    return candidate if candidate.is_dir() else None


def cmdline_has_mode(cmdline: list[str] | tuple[str, ...], mode: str) -> bool:
    """Match an exact internal launcher mode in a process command line."""
    return any(str(arg).strip().lower() == mode.lower() for arg in cmdline)
