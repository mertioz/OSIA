"""Cross-platform subprocess options for OSIA-owned process launches.

Only non-interactive Windows children are hidden.  Browser/shell-open actions,
installers, UAC prompts, and explicitly interactive commands deliberately keep
their native window behaviour.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from enum import Enum
from typing import Any


class ProcessMode(str, Enum):
    """The user-interaction contract of an OSIA child process."""

    BACKGROUND_SUPERVISED = "background_supervised"
    BACKGROUND_PROBE = "background_probe"
    RELAUNCH_COORDINATOR = "relaunch_coordinator"
    INTERACTIVE = "interactive"
    BROWSER_OPEN = "browser_open"
    INSTALLER_UAC = "installer_uac"


_VISIBLE_MODES = {
    ProcessMode.INTERACTIVE,
    ProcessMode.BROWSER_OPEN,
    ProcessMode.INSTALLER_UAC,
}
_BACKGROUND_MODES = {
    ProcessMode.BACKGROUND_SUPERVISED,
    ProcessMode.BACKGROUND_PROBE,
}


def process_kwargs(
    mode: ProcessMode,
    existing: dict[str, Any] | None = None,
    *,
    platform: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``existing`` with safe platform process options.

    Existing creation flags are preserved and merged.  Contradictory requests
    (a hidden process plus ``CREATE_NEW_CONSOLE``) fail loudly instead of
    silently combining incompatible Windows flags.  On non-Windows platforms
    Windows-only options are removed.
    """

    kwargs = dict(existing or {})
    target = sys.platform if platform is None else platform
    if target != "win32":
        kwargs.pop("creationflags", None)
        kwargs.pop("startupinfo", None)
        return kwargs
    if mode in _VISIBLE_MODES:
        return kwargs
    if kwargs.get("shell"):
        raise ValueError("background OSIA processes must use shell=False")

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    flags = int(kwargs.get("creationflags", 0) or 0)

    if new_console and flags & new_console:
        raise ValueError("CREATE_NEW_CONSOLE conflicts with a hidden OSIA process")

    if mode in _BACKGROUND_MODES:
        # DETACHED_PROCESS already prevents console inheritance; CREATE_NO_WINDOW
        # is ignored with it, so never combine the two blindly.
        if not (detached and flags & detached):
            flags |= no_window
    elif mode == ProcessMode.RELAUNCH_COORDINATOR:
        flags &= ~no_window
        flags |= detached | new_group
    else:  # Defensive if a future enum member is added without a policy.
        raise ValueError(f"unsupported process mode: {mode}")

    if flags:
        kwargs["creationflags"] = flags

    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        current = kwargs.get("startupinfo")
        startupinfo = copy.copy(current) if current is not None else startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def run_background_probe(args, **kwargs) -> subprocess.CompletedProcess:
    """Run one short non-interactive probe with observable output and exit code."""

    if kwargs.get("shell"):
        raise ValueError("background OSIA probes must use shell=False")
    if not kwargs.get("capture_output") and "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs["capture_output"] = True
    prepared = process_kwargs(ProcessMode.BACKGROUND_PROBE, kwargs)
    return subprocess.run(args, **prepared)
