#!/usr/bin/env python3
"""Windows-only supervisor for OSIA's background services (mobile server + tunnel).

This is the Windows equivalent of the Linux ``osia-server.service`` /
``osia-tunnel.service`` systemd units. It is launched by the per-user Scheduled
Tasks ("OSIA Server" / "OSIA Tunnel") that ``install.bat`` /
``osia.utils.systemd_manager.install_windows_services`` register with an AtLogOn
trigger. As a result the services:

  * start at login, **before** (and independently of) the OSIA GUI,
  * keep running when the GUI is closed, and
  * are restarted automatically by this loop if the child crashes — mirroring
    systemd's ``Restart=always`` / ``RestartSec=3`` (Task Scheduler's own
    restart-on-failure has a one-minute minimum, far slower than systemd).

Usage::

    pythonw run_service.py {server|tunnel}

Why a loop instead of relying on Task Scheduler restart-on-failure: besides the
faster restart, the loop stays compatible with the phone-triggered
``restart_server`` WebSocket command (``osia/server/server.py``), which spawns a
**detached** replacement server process and exits. When our child exits and we
detect another owner of the service already (re)starting, we defer to it instead
of racing it for port 8765.

Windowless by construction: launched via ``pythonw.exe`` and every child is
spawned with ``CREATE_NO_WINDOW`` so nothing flashes a console at login. All
output is appended to a log in the OS temp dir.
"""
import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path

# windows/run_service.py → <repo>/windows → <repo>
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_RUN_SERVER = _REPO / "osia" / "server" / "run_server.py"
_CLOUDFLARED = _HERE / "cloudflared.exe"
_TUNNEL_LOG = _HERE / "tunnel.log"

_RESTART_SEC = 3.0      # delay before relaunching a crashed child (systemd RestartSec)
_POLL_SEC = 2.0         # cadence for re-checking a live child / an external owner
_STARTUP_GRACE = 2.0    # head start before re-checking a freshly launched child

# No console window for the child console apps (python.exe / cloudflared.exe).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _venv_python() -> str:
    """The regular venv ``python.exe`` (launched CREATE_NO_WINDOW → no console).

    A regular python with its stdout redirected to the log (below) has a valid
    ``sys.stdout``, so ``run_server.py`` never hits the pythonw "stdout is None"
    crash. Falls back to a ``python.exe`` sibling of the running interpreter.
    """
    py = _REPO / "venv" / "Scripts" / "python.exe"
    if py.exists():
        return str(py)
    exe = Path(sys.executable) if sys.executable else None
    if exe is not None and exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return sys.executable or "python.exe"


def _open_log(kind: str):
    try:
        return open(
            Path(tempfile.gettempdir()) / f"osia_{kind}_service.log",
            "a", encoding="utf-8", errors="replace",
        )
    except Exception:
        return None


def _child_command(kind: str):
    """(argv, cwd) for the supervised child."""
    if kind == "server":
        return [_venv_python(), str(_RUN_SERVER)], str(_REPO)
    # tunnel — identical invocation to windows\start_tunnel.bat (named tunnel).
    return (
        [str(_CLOUDFLARED), "tunnel", "--no-autoupdate",
         "--logfile", str(_TUNNEL_LOG), "run", "osia-windows"],
        str(_HERE),
    )


def _other_owner_running(kind: str, exclude_pid: int) -> bool:
    """True if a service process other than ``exclude_pid`` / this supervisor
    already owns the service — e.g. the detached replacement spawned by the
    phone's ``restart_server`` command. Lets the loop defer rather than double
    bind port 8765. Best-effort: returns False if ``psutil`` is unavailable."""
    try:
        import psutil
    except Exception:
        return False
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info.get("pid")
            if pid in (me, exclude_pid):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if kind == "tunnel":
                # Only an existing OSIA *named* tunnel counts as another owner —
                # NOT a foreign/stale cloudflared the user runs for something else
                # (matching the bare image name would make us defer forever).
                if (proc.info.get("name") or "").lower() == "cloudflared.exe" \
                        and "osia-windows" in cmdline:
                    return True
            else:
                if "run_server.py" in cmdline:
                    return True
        except Exception:
            continue
    return False


def main() -> int:
    kind = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if kind not in ("server", "tunnel"):
        return 2

    # pythonw.exe has no real stdout/stderr; redirect ours so any print is safe
    # and the supervisor stays diagnosable.
    log = _open_log(kind)
    if log is not None:
        try:
            sys.stdout = log
            sys.stderr = log
        except Exception:
            pass

    if kind == "tunnel" and not _CLOUDFLARED.exists():
        print(f"[run_service] cloudflared.exe not found at {_CLOUDFLARED}; exiting.")
        return 1

    cmd, cwd = _child_command(kind)
    my_child = None
    while True:
        # Our child is still alive — keep watching it.
        if my_child is not None and my_child.poll() is None:
            time.sleep(_POLL_SEC)
            continue
        dead_pid = my_child.pid if my_child is not None else -1
        my_child = None
        # Did a detached replacement (restart_server) or another instance take
        # over? If so, defer to it instead of racing for the port.
        if _other_owner_running(kind, exclude_pid=dead_pid):
            time.sleep(_POLL_SEC)
            continue
        # Nobody is serving — (re)launch the child.
        try:
            my_child = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=(log or subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
            print(f"[run_service] launched {kind} child pid={my_child.pid}")
        except Exception as e:
            print(f"[run_service] failed to launch {kind}: {e}")
            time.sleep(_RESTART_SEC)
            continue
        # Give it a head start, then if it died immediately (e.g. the port was
        # still held during a restart) back off so we don't hot-loop.
        time.sleep(_STARTUP_GRACE)
        if my_child.poll() is not None:
            time.sleep(_RESTART_SEC)


if __name__ == "__main__":
    sys.exit(main())
