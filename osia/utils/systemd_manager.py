"""Service control for OSIA's GUI "Startup" toggles.

Linux: a thin wrapper around ``systemctl``. Privileged operations
(enable/disable) are routed through ``pkexec`` so they work from a desktop
session without a controlling terminal. Read-only queries (``is-enabled``,
``is-active``) don't need elevation.

Windows: there is no systemd. The two units the GUI knows about map to per-user
**Scheduled Tasks** that run independently of the OSIA GUI process — the Windows
equivalent of the Linux units:

  * ``osia-tunnel.service`` -> task "OSIA Tunnel"
  * ``osia-server.service`` -> task "OSIA Server"

Each task triggers AtLogOn and runs ``windows\\run_service.py {tunnel|server}``
(a windowless supervisor that keeps the child alive, mirroring systemd
``Restart=always``). The tasks are created by ``install.bat`` (which calls
``install_windows_services``) and can be enabled/disabled/restarted from the GUI
"Startup" toggles. Registration uses PowerShell (rich settings: run on battery,
no time limit, restart-on-failure); lifecycle queries/run/stop/delete use
``schtasks`` exit codes only — never its localized text output (the OS may be
non-English). "Currently running" is probed natively (port 8765 / cloudflared).

The public function names/signatures are unchanged across platforms so the GUI
(``osia/gui/main_popup.py``) and every other caller work without modification.
"""

import os
import subprocess
import sys
from pathlib import Path

# ── Windows detection + mappings ────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# windows/ control scripts live at <repo>/windows. This file is at
# <repo>/osia/utils/systemd_manager.py, so three parents up is the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WINDOWS_DIR = _REPO_ROOT / "windows"
_RUN_SERVICE = _WINDOWS_DIR / "run_service.py"

# Local port the OSIA mobile server listens on (see osia/server/run_server.py).
_SERVER_PORT = 8765

# Map the systemd unit names the GUI uses to a logical kind so the Windows
# implementation can pick the right control path.
_UNIT_KIND = {
    "osia-tunnel.service": "tunnel",
    "osia-server.service": "server",
}

# Scheduled Task display names (the Windows equivalent of the systemd units).
# Short, branded names so they read clearly in Task Scheduler and the Windows
# "Startup apps" list.
_TASK_NAME = {
    "tunnel": "OSIA Tunnel",
    "server": "OSIA Server",
}

# Human-readable task descriptions (shown in Task Scheduler's "Description"
# field). Task Scheduler 2.0 has no per-task icon property — the only branding
# levers are the task name (above) and this description; the icon shown in
# "Startup apps" comes from the launched executable, not the task itself.
_TASK_DESC = {
    "server": "OSIA Server — Mobile WebSocket Server",
    "tunnel": "OSIA Tunnel — Cloudflare Tunnel",
}

# Display names used by earlier WOR-47 iterations. Removed on (re)registration so
# an upgrade never leaves an orphaned AtLogOn task that would double-start the
# service at the next login.
_LEGACY_TASK_NAMES = {
    "server": ("OSIA Mobile Server",),
    "tunnel": ("OSIA Cloudflare Tunnel",),
}


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ── Windows helpers ─────────────────────────────────────────────────────────

# No console window when a GUI (pythonw) process shells out to these tools.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _schtasks(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run ``schtasks`` (native, fast). Callers must rely on the **exit code**
    only — schtasks' text output is localized and must never be parsed."""
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
        creationflags=_NO_WINDOW,
    )


def _ps(script: str, env_extra: dict, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet, passing values via env vars (no quoting hell)."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout, check=False,
        env=env, creationflags=_NO_WINDOW,
    )


def _win_proc_running(image: str) -> bool:
    """True if a process with executable name ``image`` is running (tasklist)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return False
    return image.lower() in (result.stdout or "").lower()


def _win_port_open(port: int) -> bool:
    """True if something accepts TCP connections on 127.0.0.1:port.

    A local port either accepts or refuses near-instantly; the short timeout
    only bounds the rare "filtered/no-response" case so a status probe can never
    stall for a full second (WOR perf fix).
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except Exception:
        return False


def _win_pythonw() -> str:
    """Prefer the venv's windowless pythonw.exe; fall back to the running one."""
    pythonw = _REPO_ROOT / "venv" / "Scripts" / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else (sys.executable or "pythonw.exe")


def _win_current_user() -> str:
    """``DOMAIN\\User`` for the current session (for the task trigger/principal)."""
    dom = os.environ.get("USERDOMAIN", "")
    usr = os.environ.get("USERNAME", "")
    return f"{dom}\\{usr}" if dom and usr else (usr or "")


# ── Scheduled-task lifecycle (Windows) ──────────────────────────────────────

def _win_task_registered(task: str) -> bool:
    """True if the Scheduled Task exists (locale-independent: exit code only)."""
    try:
        return _schtasks(["/query", "/TN", task]).returncode == 0
    except Exception:
        return False


def _win_task_running(task: str) -> bool:
    """True if the Scheduled Task is currently in the Running state.

    Uses the PowerShell ``Get-ScheduledTask`` ``.State`` enum, which is both
    locale-independent (fr-FR/any) and psutil-independent — so it backstops the
    psutil-based supervisor probe and keeps the duplicate-start guard from ever
    failing open. Called only on infrequent paths (install / GUI toggle /
    startup self-heal), never the periodic status refresh.
    """
    if not task:
        return False
    try:
        r = _ps(
            "$t = Get-ScheduledTask -TaskName $env:OSIA_TASK"
            " -ErrorAction SilentlyContinue;"
            " if ($t -and $t.State -eq 'Running') { exit 0 } else { exit 1 }",
            {"OSIA_TASK": task}, timeout=15.0,
        )
        return r.returncode == 0
    except Exception:
        return False


def _win_register_task(kind: str) -> bool:
    """Create/overwrite the per-user Scheduled Task for ``kind`` (server|tunnel).

    AtLogOn trigger; runs the windowless ``run_service.py`` supervisor; no
    execution time limit, runs on battery, restart-on-failure — the closest
    Task Scheduler analogue to the Linux unit's ``Restart=always``. Needs no
    admin (LeastPrivilege / InteractiveToken for the current user). A branded
    name + description make it legible in Task Scheduler / "Startup apps".
    """
    task = _TASK_NAME.get(kind)
    if not task:
        return False
    # Drop any pre-rename task with the old display name so an upgrade never
    # leaves an orphan that also fires AtLogOn (best-effort; no-op if absent).
    for legacy in _LEGACY_TASK_NAMES.get(kind, ()):
        _win_delete_task(legacy)
    user = _win_current_user()
    script = (
        "$ErrorActionPreference='Stop';"
        "$a = New-ScheduledTaskAction -Execute $env:OSIA_PYW"
        " -Argument $env:OSIA_ARG -WorkingDirectory $env:OSIA_REPO;"
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:OSIA_USER;"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -MultipleInstances IgnoreNew -StartWhenAvailable"
        " -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1);"
        "$p = New-ScheduledTaskPrincipal -UserId $env:OSIA_USER"
        " -LogonType Interactive -RunLevel Limited;"
        "Register-ScheduledTask -TaskName $env:OSIA_TASK -Action $a -Trigger $t"
        " -Settings $s -Principal $p -Description $env:OSIA_DESC -Force | Out-Null"
    )
    try:
        r = _ps(script, {
            "OSIA_PYW": _win_pythonw(),
            "OSIA_ARG": f'"{_RUN_SERVICE}" {kind}',
            "OSIA_REPO": str(_REPO_ROOT),
            "OSIA_USER": user,
            "OSIA_TASK": task,
            "OSIA_DESC": _TASK_DESC.get(kind, task),
        })
    except Exception as e:
        print(f"[win-service] register {task} failed: {e}")
        return False
    if r.returncode != 0:
        print(f"[win-service] register {task} failed: {(r.stderr or r.stdout).strip()}")
    return r.returncode == 0


def _win_run_task(task: str) -> bool:
    try:
        return _schtasks(["/run", "/TN", task]).returncode == 0
    except Exception:
        return False


def _win_end_task(task: str) -> bool:
    """End any running instances of the task (terminates the supervisor + its
    child, which share the task's job object). Best-effort."""
    try:
        _schtasks(["/end", "/TN", task])
    except Exception:
        return False
    return True


def _win_delete_task(task: str) -> bool:
    """Unregister the task so it no longer starts at login. Best-effort."""
    try:
        _schtasks(["/delete", "/TN", task, "/F"])
    except Exception:
        return False
    return True


def _win_service_up(kind: str) -> bool:
    """True if the *service itself* is currently up (not just the supervisor)."""
    if kind == "server":
        return _win_port_open(_SERVER_PORT)
    if kind == "tunnel":
        return _win_proc_running("cloudflared.exe")
    return False


def _win_supervisor_running(kind: str) -> bool:
    """True if our run_service.py supervisor for ``kind`` is already alive.

    Lets us avoid launching a second task instance during the server's slow
    (torch import) startup window, when the port isn't open yet but the
    supervisor already is. Best-effort (psutil); returns False if unavailable.
    """
    try:
        import psutil
    except Exception:
        return False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue
        joined = " ".join(cmdline)
        if "run_service.py" in joined and kind in cmdline:
            return True
    return False


def _win_should_start(kind: str) -> bool:
    """Only start a task instance when nothing is already serving/starting —
    avoids a second instance racing for the port (the ``IgnoreNew`` policy is not
    reliably honoured by all Windows builds).

    Three independent signals so the guard never fails open (e.g. if psutil is
    unavailable): the service port/process is up, our supervisor process is alive
    (psutil), or the Scheduled Task itself is Running (psutil-independent enum).
    """
    if _win_service_up(kind):
        return False
    if _win_supervisor_running(kind):
        return False
    if _win_task_running(_TASK_NAME.get(kind, "")):
        return False
    return True


def _win_stop_server() -> bool:
    """Terminate the running OSIA mobile server process(es), waiting for them to
    actually exit so a follow-up start (restart_service) finds port 8765 free.

    Targets the run_server.py child, NOT the run_service.py supervisor (which is
    stopped via the task's job object by ``_win_end_task``)."""
    try:
        import psutil
    except Exception as e:
        print(f"[win-service] psutil unavailable, cannot stop server: {e}")
        return False
    targets = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if "run_server.py" in cmdline or "osia.server" in cmdline:
            try:
                proc.terminate()
                targets.append(proc)
            except Exception:
                pass
    if not targets:
        return False
    # terminate() is asynchronous on Windows; wait for the port to be released
    # and escalate to kill() on any straggler so restart can rebind 8765.
    try:
        _gone, alive = psutil.wait_procs(targets, timeout=5)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass
    return True


def _win_kill_tunnel() -> bool:
    """Terminate the OSIA tunnel's leftover cloudflared.exe (belt-and-suspenders
    alongside ending the task).

    Targets ONLY the named ``osia-windows`` tunnel by cmdline (like
    ``_win_stop_server`` matches the server), so unrelated cloudflared instances
    the user runs for other projects are never collaterally killed. No-op when
    psutil is unavailable — the task's job object already reaped the child."""
    try:
        import psutil
    except Exception:
        return False
    killed = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() != "cloudflared.exe":
                continue
            if "osia-windows" in " ".join(proc.info.get("cmdline") or []):
                proc.terminate()
                killed.append(proc)
        except Exception:
            continue
    if killed:
        try:
            _gone, alive = psutil.wait_procs(killed, timeout=5)
            for p in alive:
                try:
                    p.kill()
                except Exception:
                    pass
        except Exception:
            pass
    return bool(killed)


def _win_kill_leftovers(kind: str) -> None:
    if kind == "server":
        _win_stop_server()
    elif kind == "tunnel":
        _win_kill_tunnel()


def _win_enable(name: str) -> bool:
    """Register the task (start at login) and start it now if not already up."""
    kind = _UNIT_KIND.get(name)
    if kind not in ("server", "tunnel"):
        print(f"[win-service] unknown unit: {name}")
        return False
    ok = _win_register_task(kind)
    if _win_should_start(kind):
        _win_run_task(_TASK_NAME[kind])
    return ok


def _win_disable(name: str) -> bool:
    """Stop the running service and unregister the task (no longer at login)."""
    kind = _UNIT_KIND.get(name)
    if kind not in ("server", "tunnel"):
        print(f"[win-service] unknown unit: {name}")
        return False
    task = _TASK_NAME[kind]
    _win_end_task(task)      # stop the supervisor + its child (task job object)
    _win_delete_task(task)   # so it won't relaunch at the next login
    _win_kill_leftovers(kind)  # reap anything the job object didn't
    return True


# ── Public API (cross-platform) ─────────────────────────────────────────────

def install_windows_services() -> bool:
    """Register (and start) both OSIA background services as per-user Scheduled
    Tasks so they run independently of the GUI and start at login. Called by
    ``install.bat``. Windows-only; a harmless no-op (returns True) elsewhere.

    Returns True only if both tasks registered successfully.
    """
    if not _IS_WINDOWS:
        return True
    ok = True
    for kind in ("server", "tunnel"):
        if not _win_register_task(kind):
            ok = False
            continue
        if _win_should_start(kind):
            _win_run_task(_TASK_NAME[kind])
    return ok


def ensure_service_running(name: str) -> bool:
    """Ensure ``name``'s Scheduled Task is registered and the service is running,
    without re-registering a task that already exists. Used by the GUI startup
    self-heal (``osia/main.py``) so the server comes up even on an install that
    predates the task model. Windows-only; no-op (returns True) elsewhere.
    """
    if not _IS_WINDOWS:
        return True
    kind = _UNIT_KIND.get(name)
    if kind not in ("server", "tunnel"):
        return False
    task = _TASK_NAME[kind]
    if not _win_task_registered(task):
        _win_register_task(kind)
    if _win_should_start(kind):
        _win_run_task(task)
    return True


def is_service_enabled(name: str) -> bool:
    """True if the unit is enabled (will start at boot/login).

    Windows: the Scheduled Task being registered means it triggers at login
    (``disable_service`` unregisters it), so the GUI toggle reflects reality.
    """
    if _IS_WINDOWS:
        kind = _UNIT_KIND.get(name)
        if kind not in ("server", "tunnel"):
            return False
        return _win_task_registered(_TASK_NAME[kind])
    try:
        result = _run(["systemctl", "is-enabled", name])
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.stdout.strip() == "enabled"


def get_service_status(name: str) -> str:
    """Return one of 'running', 'stopped', 'error'."""
    if _IS_WINDOWS:
        kind = _UNIT_KIND.get(name)
        if kind == "tunnel":
            return "running" if _win_proc_running("cloudflared.exe") else "stopped"
        if kind == "server":
            return "running" if _win_port_open(_SERVER_PORT) else "stopped"
        return "error"
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
    if _IS_WINDOWS:
        return _win_enable(name)
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
    if _IS_WINDOWS:
        return _win_disable(name)
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
    if _IS_WINDOWS:
        kind = _UNIT_KIND.get(name)
        if kind not in ("server", "tunnel"):
            return False
        task = _TASK_NAME[kind]
        # Stop the running service (and reap leftovers so the port/binary is
        # free) WITHOUT unregistering the task, then start it again.
        _win_end_task(task)
        _win_kill_leftovers(kind)
        if not _win_task_registered(task):
            _win_register_task(kind)  # self-heal if it was never installed
        return _win_run_task(task)
    try:
        result = _run(
            ["pkexec", "systemctl", "restart", name],
            timeout=60.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0
