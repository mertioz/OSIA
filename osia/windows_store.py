"""Integration with the OSIA package registered by the Microsoft Store."""
from functools import cache
import os
import subprocess
import sys

from osia.runtime import SERVICE_MODE, application_executable, is_frozen

PACKAGE_FAMILY = "GreatInvention.OSIA_ge7crsndb7e8c"
PRODUCT_ID = "9P2FX4QK5Q2J"
GUI_STARTUP = "OsiaDesktopStartup"
SERVER_STARTUP = "OsiaServerStartup"


@cache
def is_store_install() -> bool:
    if sys.platform != "win32" or not is_frozen():
        return False
    import ctypes
    from ctypes import wintypes

    length = wintypes.UINT(128)
    family = ctypes.create_unicode_buffer(length.value)
    get_family = ctypes.windll.kernel32.GetCurrentPackageFamilyName
    get_family.argtypes = (ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
    get_family.restype = wintypes.LONG
    return get_family(ctypes.byref(length), family) == 0 and family.value == PACKAGE_FAMILY


def supervisor_shutdown_event(kind: str) -> str:
    name = "OSIA.Store" if is_store_install() else "OSIA"
    return f"Local\\{name}.SupervisorShutdown.{kind}"


def startup_task(task: str, enabled: bool | None = None) -> bool:
    """Read or change the declared Windows startup task. Respect OS/user policy."""
    if not is_store_install() or task not in (GUI_STARTUP, SERVER_STARTUP):
        return False
    helper = application_executable().with_name("OSIA.StoreStartup.exe")
    action = "state" if enabled is None else ("enable" if enabled else "disable")
    try:
        result = subprocess.run([str(helper), task, action], timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        if result.returncode == 3 and enabled:
            os.startfile("ms-settings:startupapps")
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_server() -> bool:
    """Start the existing supervision entry point from the current package."""
    exe = application_executable()
    try:
        subprocess.Popen([str(exe), SERVICE_MODE, "server"], cwd=exe.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except OSError:
        return False
