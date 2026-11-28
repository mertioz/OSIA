"""Integration with the OSIA package registered by the Microsoft Store."""
from functools import cache
import os
from pathlib import PureWindowsPath
import re
import subprocess
import sys

from osia.runtime import SERVICE_MODE, application_executable, is_frozen

PACKAGE_FAMILY = "GreatInvention.OSIA_ge7crsndb7e8c"
PRODUCT_ID = "9P2FX4QK5Q2J"
GUI_STARTUP = "OsiaDesktopStartup"
SERVER_STARTUP = "OsiaServerStartup"
# Keep the purpose stable across product updates. A broader access purpose gets its own key.
MOBILE_NETWORK_PERMISSION = "paired_mobile_lan_webrtc_v1"


def permission_decision(permission: str) -> bool | None:
    from osia.config import get_setting
    choices = get_setting("desktop_permission_decisions", {})
    value = choices.get(permission) if isinstance(choices, dict) else None
    return value if type(value) is bool else None


def remember_permission(permission: str, granted: bool) -> None:
    from osia.config import get_setting, set_setting, settings_transaction
    if type(granted) is not bool:
        raise ValueError("A permission decision must be explicit")
    with settings_transaction():
        choices = get_setting("desktop_permission_decisions", {})
        choices = dict(choices) if isinstance(choices, dict) else {}
        choices[permission] = granted
        set_setting("desktop_permission_decisions", choices)


def mobile_network_allowed() -> bool:
    return not is_store_install() or permission_decision(MOBILE_NETWORK_PERMISSION) is True


def previous_firewall_choice(rules, apps_directory):
    """Reuse an equivalent native decision for the latest known OSIA package only."""
    profiles = {"domain", "private", "public"}
    latest = ()
    decisions = {}
    package = re.compile(r"greatinvention\.osia_([0-9]+(?:\.[0-9]+){3})_x64__ge7crsndb7e8c", re.I)
    for identity, record in rules:
        if not identity.lower().startswith(("tcp query user{", "udp query user{")):
            continue
        fields = [entry.split("=", 1) for entry in record.split("|") if "=" in entry]
        values = dict(fields)
        path = PureWindowsPath(values.get("App", ""))
        if (len(path.parents) < 3 or path.parent.name.lower() != "app" or path.name.lower() != "osia.exe"
                or path.parents[2] != apps_directory):
            continue
        match = package.fullmatch(path.parents[1].name)
        if not match or values.get("Dir") != "In" or values.get("Active") != "TRUE":
            continue
        version = tuple(int(n) for n in match[1].split("."))
        if version < latest:
            continue
        if version > latest:
            latest = version
            decisions.clear()
        if any(k in values for k in ("LPort", "RPort", "RA4", "RA6", "LA4", "LA6", "Svc", "Security")):
            continue
        protocol = values.get("Protocol")
        action = values.get("Action")
        if protocol not in ("6", "17") or action not in ("Allow", "Block"):
            continue
        entries = {v.lower() for k, v in fields if k == "Profile"}
        for profile in (profiles if "all" in entries else profiles & entries):
            key = (profile, protocol)
            decisions[key] = decisions.get(key, True) and action == "Allow"
    if len(decisions) == 6 and len(set(decisions.values())) == 1:
        return next(iter(decisions.values()))
    return None


def equivalent_mobile_firewall_choice():
    """Read prior OS decisions; partial permissions cannot authorize wider access."""
    import winreg
    rules = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, (
            r"SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters\FirewallPolicy\FirewallRules"
        )) as key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, data, kind = winreg.EnumValue(key, index)
                if kind == winreg.REG_SZ and "greatinvention.osia_" in name.lower():
                    rules.append((name, data))
    except OSError:
        return None
    return previous_firewall_choice(rules, PureWindowsPath(application_executable()).parents[2])


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
