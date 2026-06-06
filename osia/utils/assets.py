#!/usr/bin/env python3
"""Resolve bundled asset paths from the project root.

Assets live in ``<project_root>/assets``. Paths are resolved from this
module's own location (not the current working directory) so the GUI finds
its icon no matter where it is launched from — a shell in any directory, a
systemd unit, or a desktop launcher.
"""

import os
import platform
import sys

# <root>/osia/utils/assets.py → <root>
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")

# The OSIA logo — shown inside the GUI (popup header, Dynamic Island idle
# state), where the surrounding transparent padding is harmless.
LOGO_PATH = os.path.join(ASSETS_DIR, "osia_logo.png")

# A tightly-cropped, square variant of the logo for the application/window/
# taskbar/dock icon. The full logo has heavy transparent padding around the
# swirl, which makes it render tiny in a fixed-size taskbar slot; this crop
# fills ~90% of its canvas so the swirl shows at a proper visible size.
ICON_PATH = os.path.join(ASSETS_DIR, "osia_icon.png")


def asset_path(name: str) -> str:
    """Absolute path to an asset bundled under ``<project_root>/assets``."""
    return os.path.join(ASSETS_DIR, name)


def ensure_desktop_entry() -> str | None:
    """Best-effort install of an ``osia.desktop`` entry.

    On Wayland/XCB the compositor maps a window to its taskbar icon + name
    through a desktop file whose basename matches
    ``QGuiApplication.setDesktopFileName("osia")`` — i.e. ``osia.desktop``.
    Without it the taskbar falls back to "python"/the script name and a
    generic icon.

    Writes ``~/.local/share/applications/osia.desktop`` pointing at the
    bundled logo, rewriting it only when the content has changed. Returns
    the written path, or ``None`` on any failure — this must never raise,
    the app has to start regardless.
    """
    try:
        apps_dir = os.path.join(
            os.path.expanduser("~"), ".local", "share", "applications"
        )
        os.makedirs(apps_dir, exist_ok=True)
        dest = os.path.join(apps_dir, "osia.desktop")

        # Prefer the project's venv interpreter so the launcher works from a
        # menu; fall back to whatever is running this process.
        py = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
        if not os.path.exists(py):
            py = sys.executable or "python3"

        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=OSIA\n"
            "Comment=OSIA — Local AI Voice Assistant\n"
            f"Exec={py} {os.path.join(PROJECT_ROOT, 'osia', 'main.py')}\n"
            f"Icon={ICON_PATH}\n"
            "Terminal=false\n"
            "Categories=Utility;\n"
            # Matches the WM_CLASS/app-id Qt advertises via
            # setDesktopFileName("osia"), so X11/Wayland resolve the icon.
            "StartupWMClass=osia\n"
        )

        try:
            with open(dest, "r") as f:
                if f.read() == content:
                    return dest
        except OSError:
            pass

        with open(dest, "w") as f:
            f.write(content)
        return dest
    except Exception:
        return None


# ── Launch-at-login autostart (WOR-22) ──────────────────────────────────────
# Separate from ensure_desktop_entry(): that installs the app menu launcher;
# this manages whether OSIA starts automatically when the user logs in. On
# Linux that is a ``~/.config/autostart/osia.desktop`` file (the XDG autostart
# spec). Windows/Mac are stubs for now.

def _autostart_desktop_path() -> str:
    """Path to the XDG autostart entry that launches OSIA at login (Linux)."""
    return os.path.join(
        os.path.expanduser("~"), ".config", "autostart", "osia.desktop"
    )


def autostart_enabled() -> bool:
    """Return True when OSIA is configured to launch at login.

    The state is derived from the autostart entry actually existing (Linux),
    so the settings toggle always reflects reality. Non-Linux platforms have
    no implementation yet and report disabled.
    """
    if platform.system() == "Linux":
        return os.path.exists(_autostart_desktop_path())
    return False


def set_autostart(enabled: bool) -> bool:
    """Enable or disable launching OSIA (wake mode) at login.

    Linux: write / remove ``~/.config/autostart/osia.desktop`` launching
    ``osia/main.py --wake`` through the project venv with ``QT_QPA_PLATFORM=xcb``
    forced (the GUI always runs under XCB). Returns True on success.

    Windows / Mac: logs that autostart is not yet implemented and returns
    False so the caller can revert the toggle. Never raises.
    """
    system = platform.system()
    if system != "Linux":
        if system == "Windows":
            print("[autostart] not yet implemented on Windows")
        elif system == "Darwin":
            print("[autostart] not yet implemented on Mac")
        else:
            print(f"[autostart] not yet implemented on {system}")
        return False

    path = _autostart_desktop_path()
    try:
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Prefer the project venv interpreter so the autostart launch uses
            # the same Python/deps as a manual launch; fall back to whatever is
            # running this process.
            py = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
            if not os.path.exists(py):
                py = sys.executable or "python3"
            main_py = os.path.join(PROJECT_ROOT, "osia", "main.py")
            # .desktop Exec= does not run a shell, so the QT_QPA_PLATFORM
            # environment variable is set via `env` (a bare `VAR=val cmd`
            # prefix would be parsed as a binary name, not an assignment).
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=OSIA\n"
                "Comment=OSIA — Local AI Voice Assistant\n"
                f"Exec=env QT_QPA_PLATFORM=xcb {py} {main_py} --wake\n"
                f"Icon={ICON_PATH}\n"
                "Terminal=false\n"
                "Categories=Utility;\n"
                "StartupWMClass=osia\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            with open(path, "w") as f:
                f.write(content)
        else:
            if os.path.exists(path):
                os.remove(path)
        return True
    except Exception as e:
        print(f"[autostart] failed to {'enable' if enabled else 'disable'}: {e}")
        return False
