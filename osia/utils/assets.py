#!/usr/bin/env python3
"""Resolve bundled asset paths from the project root.

Assets live in ``<project_root>/assets``. Paths are resolved from this
module's own location (not the current working directory) so the GUI finds
its icon no matter where it is launched from — a shell in any directory, a
systemd unit, or a desktop launcher.
"""

import os
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
