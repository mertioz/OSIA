#!/usr/bin/env python3
"""Resolve bundled asset paths from the project root.

Assets live in ``<project_root>/assets``. Paths are resolved from this
module's own location (not the current working directory) so the GUI finds
its icon no matter where it is launched from — a shell in any directory, a
systemd unit, or a desktop launcher.
"""

import os
import platform
import plistlib
import sys
import tempfile
from configparser import ConfigParser, Error as ConfigError
from pathlib import Path
from xml.parsers.expat import ExpatError

from osia import windows_store
from osia.runtime import application_executable, is_frozen, portable_desktop_executable, resource_root

# <root>/osia/utils/assets.py → <root>
PROJECT_ROOT = str(resource_root())
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
    if platform.system() != "Linux" or portable_desktop_executable() is not None:
        return None
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
            with open(dest, "r", encoding="utf-8", errors="replace") as f:
                if f.read() == content:
                    return dest
        except OSError:
            pass

        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        return dest
    except Exception:
        return None


# ── Launch-at-login autostart (WOR-22, Windows in WOR-11) ───────────────────
# Separate from ensure_desktop_entry(): that installs the app menu launcher;
# this manages whether OSIA starts automatically when the user logs in.
#   • Linux:   a ``~/.config/autostart/osia.desktop`` file (XDG autostart spec)
#   • Windows: an ``OSIA`` value under the per-user Run key
#              ``HKCU\Software\Microsoft\Windows\CurrentVersion\Run``
#   • macOS:   a per-user launchd agent limited to the graphical login session

# Windows per-user "run at login" registry location.
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_RUN_VALUE = "OSIA"


def _autostart_desktop_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    return config_home / "autostart" / "osia.desktop"


def _unix_startup_arguments() -> list[str]:
    portable = portable_desktop_executable()
    if portable is not None:
        return [str(portable)]
    project = Path(PROJECT_ROOT)
    release = project.parent
    if project.name == "app" and application_executable().is_relative_to(release / "python"):
        launcher = release / "bin" / "osia"
        if launcher.is_file():
            stable = Path.home() / ".local" / "bin" / "osia"
            if stable.is_file() and stable.samefile(launcher):
                launcher = stable
            return [str(launcher)]
    python = project / "venv" / "bin" / "python"
    return [str(python) if python.is_file() else (sys.executable or "python3"),
            str(project / "osia" / "main.py"), "--wake"]


def _xdg_argument(value: str) -> str:
    # The desktop-entry string escapes are decoded before Exec argument quoting.
    escaped = "".join("\\" + c if c in '\\"$`' else c for c in value)
    escaped = (escaped.replace("\\", "\\\\").replace("%", "%%")
               .replace("\n", r"\n").replace("\r", r"\r").replace("\t", r"\t"))
    return '"' + escaped + '"'


def _write_login_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _mac_launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "space.osia.desktop.login.plist"


def _windows_autostart_command() -> str:
    """Command Windows runs at login to launch OSIA in wake mode.

    Prefers the project venv's windowless ``pythonw.exe`` (so no console flashes
    at login); falls back to the running interpreter (``sys.executable``). Both
    the interpreter and ``main.py`` are derived from this module's own location
    (``PROJECT_ROOT`` / ``Path(__file__)``), never a hardcoded path — WOR-11.
    """
    if is_frozen():
        return f'"{application_executable()}" --wake'
    main_py = os.path.join(PROJECT_ROOT, "osia", "main.py")
    pythonw = os.path.join(PROJECT_ROOT, "venv", "Scripts", "pythonw.exe")
    py = pythonw if os.path.exists(pythonw) else (sys.executable or "pythonw.exe")
    # Quote both paths so spaces (e.g. "C:\\Users\\My Name\\...") survive.
    return f'"{py}" "{main_py}" --wake'


def _set_autostart_windows(enabled: bool) -> bool:
    """Add/remove the ``OSIA`` value under the HKCU Run key. Returns True on
    success, False on any failure (never raises). Disabling is idempotent."""
    try:
        import winreg  # Windows-only stdlib module
    except Exception as e:
        print(f"[autostart] winreg unavailable: {e}")
        return False
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE,
        )
        try:
            if enabled:
                winreg.SetValueEx(
                    key, _WIN_RUN_VALUE, 0, winreg.REG_SZ,
                    _windows_autostart_command(),
                )
            else:
                try:
                    winreg.DeleteValue(key, _WIN_RUN_VALUE)
                except FileNotFoundError:
                    pass  # already absent — nothing to disable
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as e:
        print(f"[autostart] Windows registry update failed: {e}")
        return False


def _windows_run_registered() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
            winreg.QueryValueEx(key, _WIN_RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


def autostart_enabled() -> bool:
    """Read the current user's login setting from the native backend."""
    try:
        if windows_store.is_store_install():
            return windows_store.startup_task(windows_store.GUI_STARTUP)
        system = platform.system()
        if system == "Windows":
            return _windows_run_registered()
        if system == "Darwin":
            with _mac_launch_agent().open("rb") as handle:
                entry = plistlib.load(handle)
            return isinstance(entry, dict) and entry.get("RunAtLoad") is True and not entry.get("Disabled", False)
        if system == "Linux":
            parser = ConfigParser(interpolation=None, strict=False)
            with _autostart_desktop_path().open(encoding="utf-8") as handle:
                parser.read_file(handle)
            entry = parser["Desktop Entry"]
            return (bool(entry.get("Exec")) and not entry.getboolean("Hidden", False)
                    and entry.getboolean("X-GNOME-Autostart-enabled", True))
    except (ImportError, OSError, ValueError, KeyError, ConfigError, ExpatError, plistlib.InvalidFileException):
        pass
    return False


def _configure_autostart(enabled: bool, *, initial: bool = False) -> bool:
    if windows_store.is_store_install():
        # Windows applies the manifest default and owns user/policy overrides.
        return initial or windows_store.startup_task(windows_store.GUI_STARTUP, enabled)
    system = platform.system()
    if system == "Windows":
        if initial:
            # Inno records the operator's actual choice, including /NOINTEGRATION.
            if is_frozen() and any(p.with_suffix(".exe").is_file()
                                   for p in application_executable().parent.glob("unins[0-9][0-9][0-9].dat")):
                return True
            if _windows_run_registered():
                return True
        return _set_autostart_windows(enabled)
    if system == "Darwin":
        path = _mac_launch_agent()
    elif system == "Linux":
        path = _autostart_desktop_path()
    else:
        return False
    if not enabled:
        path.unlink(missing_ok=True)
        return True
    if initial and os.path.lexists(path):
        # A Hidden=true XDG entry, for example, is a deliberate user choice.
        return True
    arguments = _unix_startup_arguments()
    if system == "Darwin":
        if "AppTranslocation" in Path(arguments[0]).parts:
            # Gatekeeper's temporary path disappears. Retry at the next normal launch.
            return False
        content = plistlib.dumps({"Label": path.stem, "ProgramArguments": arguments,
                                  "RunAtLoad": True, "LimitLoadToSessionType": "Aqua",
                                  "AssociatedBundleIdentifiers": ["space.osia.desktop"]})
    else:
        command = " ".join(_xdg_argument(arg) for arg in arguments)
        content = ("[Desktop Entry]\nType=Application\nName=OSIA\n"
                   f"Exec=env QT_QPA_PLATFORM=xcb {command}\n"
                   "Terminal=false\nStartupWMClass=osia\nX-GNOME-Autostart-enabled=true\n").encode()
    _write_login_file(path, content)
    return True


def initialize_autostart() -> bool:
    """Apply the login default once, on the first desktop launch of a new profile."""
    from osia import config

    try:
        pending = config.desktop_autostart_pending_file()
        if not pending.is_file() or not Path(config.SETTINGS_FILE).is_file():
            return True
        with config.settings_transaction():
            if not pending.is_file():
                return True
            if not _configure_autostart(True, initial=True):
                return False
            pending.unlink(missing_ok=True)
        return True
    except (ImportError, OSError, ValueError) as exc:
        print(f"[autostart] login setup deferred: {type(exc).__name__}")
        return False


def set_autostart(enabled: bool) -> bool:
    """Apply a user's explicit choice, also cancelling any pending default."""
    from osia import config

    try:
        pending = config.desktop_autostart_pending_file()
        if pending.is_file() or not Path(config.SETTINGS_FILE).exists():
            with config.settings_transaction():
                if not Path(config.SETTINGS_FILE).exists():
                    config.ensure_config_files(create_token=False)
                pending.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[autostart] could not record manual choice: {type(exc).__name__}")
    try:
        return _configure_autostart(enabled)
    except (ImportError, OSError, ValueError) as exc:
        print(f"[autostart] login setting update failed: {type(exc).__name__}")
        return False
