"""Service control for OSIA's GUI "Startup" toggles.

Linux: a thin wrapper around ``systemctl``. Privileged operations
(enable/disable) are routed through ``pkexec`` so they work from a desktop
session without a controlling terminal. Read-only queries (``is-enabled``,
``is-active``) don't need elevation.

Windows: there is no systemd. ``osia-server.service`` maps to the per-user
``OSIA Server`` Scheduled Task, which runs independently of the GUI and starts
``windows\\run_service.py server`` at logon. Historical ``OSIA Tunnel`` task
names and process detection remain here only so upgrades and uninstall can
remove the retired quick-tunnel installation without touching unrelated
cloudflared processes.

The public function names/signatures are unchanged across platforms so the GUI
(``osia/gui/main_popup.py``) and every other caller work without modification.
"""

import os
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from osia.config import CONFIG_DIR
from osia.runtime import (
    SERVER_MODE,
    SERVICE_MODE,
    WAKE_RECOVERY_MODE,
    application_dir,
    application_executable,
    cmdline_has_mode,
    is_frozen,
    resource_root,
)
from osia.utils.subprocess_policy import run_background_probe

# ── Windows detection + mappings ────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# windows/ control scripts live at <repo>/windows. This file is at
# <repo>/osia/utils/systemd_manager.py, so three parents up is the repo root.
_REPO_ROOT = resource_root()
_WINDOWS_DIR = _REPO_ROOT / "windows"
_RUN_SERVICE = _WINDOWS_DIR / "run_service.py"
_WAKE_RECOVERY = _WINDOWS_DIR / "wake_recovery.py"
_APP_EXE = application_executable()
_APP_DIR = application_dir()
_SERVER_DISABLED_MARKER = Path(CONFIG_DIR) / "windows_server_disabled"
# Same path windows\run_service.py passes to cloudflared (--logfile). It is the
# OSIA-specific signature used to kill exactly OSIA's quick tunnel (WOR-64).
_TUNNEL_LOG = _WINDOWS_DIR / "tunnel.log"

# Local port the OSIA mobile server listens on (see osia/server/run_server.py).
_SERVER_PORT = 8765
_SERVER_PORTS = (8765, 8766)

_WIN_RESTART_MUTEX_TIMEOUT = 30.0
_WIN_LIFECYCLE_MUTEX_TIMEOUT = 5.0
_WIN_GRACEFUL_STOP_TIMEOUT = 12.0
_WIN_FORCED_STOP_TIMEOUT = 20.0
_WIN_STOP_POLL_SEC = 0.5

_WIN_WAIT_OBJECT_0 = 0x00000000
_WIN_WAIT_ABANDONED = 0x00000080
_WIN_WAIT_TIMEOUT = 0x00000102
_JOB_OBJECT_TERMINATE = 0x0008

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

_WAKE_TASK_NAME = "OSIA Wake Recovery"
_WAKE_TASK_DESC = "OSIA Wake Recovery - restart server after Windows resume"

# Display names used by earlier WOR-47 iterations. Removed on (re)registration so
# an upgrade never leaves an orphaned AtLogOn task that would double-start the
# service at the next login.
_LEGACY_TASK_NAMES = {
    "server": ("OSIA Mobile Server",),
    "tunnel": ("OSIA Cloudflare Tunnel",),
}


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return run_background_probe(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ── Windows helpers ─────────────────────────────────────────────────────────

_KERNEL32 = None


def _kernel32():
    """Configured kernel32 synchronization API, loaded only on Windows."""
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    if sys.platform != "win32":
        raise OSError("Windows lifecycle primitives are unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                        wintypes.LPCWSTR]
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    _KERNEL32 = kernel32
    return kernel32


def _win_shutdown_event_name(kind: str) -> str:
    return f"Local\\OSIA.SupervisorShutdown.{kind}"


def _win_lifecycle_mutex_name(kind: str) -> str:
    return f"Local\\OSIA.SupervisorLifecycle.{kind}"


def _win_restart_mutex_name(kind: str) -> str:
    return f"Local\\OSIA.ServiceRestart.{kind}"


def _win_child_job_name(kind: str, supervisor_pid: int) -> str:
    return f"Local\\OSIA.SupervisorChildren.{kind}.{supervisor_pid}"


class _WinNamedMutex:
    """Named interprocess mutex with bounded and abandoned-owner handling."""

    def __init__(self, name: str):
        self._kernel32 = _kernel32()
        self._handle = self._kernel32.CreateMutexW(None, False, name)
        self._acquired = False
        self.abandoned = False
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")

    def acquire(self, timeout: float) -> bool:
        millis = max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = self._kernel32.WaitForSingleObject(self._handle, millis)
        if result in (_WIN_WAIT_OBJECT_0, _WIN_WAIT_ABANDONED):
            self._acquired = True
            self.abandoned = result == _WIN_WAIT_ABANDONED
            return True
        if result == _WIN_WAIT_TIMEOUT:
            return False
        raise OSError(ctypes.get_last_error(), "mutex wait failed")

    def release(self) -> None:
        if self._handle and self._acquired:
            self._kernel32.ReleaseMutex(self._handle)
            self._acquired = False

    def close(self) -> None:
        self.release()
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _WinShutdownSignal:
    """Hold a manual-reset event signaled until the old tree is fully gone."""

    def __init__(self, kind: str):
        self._kernel32 = _kernel32()
        self._handle = self._kernel32.CreateEventW(
            None, True, False, _win_shutdown_event_name(kind),
        )
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateEventW failed")
        if not self._kernel32.SetEvent(self._handle):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetEvent failed")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


@dataclass
class _WinServiceTree:
    """PID/create-time identities proven to belong to old OSIA supervisors."""

    processes: dict[int, float] = field(default_factory=dict)
    supervisors: set[int] = field(default_factory=set)


def _schtasks(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run ``schtasks`` (native, fast). Callers must rely on the **exit code**
    only — schtasks' text output is localized and must never be parsed."""
    return run_background_probe(
        ["schtasks", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _ps(script: str, env_extra: dict, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet, passing values via env vars (no quoting hell)."""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    return run_background_probe(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout, check=False,
        env=env,
    )


def _win_norm_cmdline(cmdline: str) -> str:
    return (cmdline or "").lower().replace("/", "\\")


def _win_cmdline_has_path(cmdline: str, path: Path) -> bool:
    return str(path).lower().replace("/", "\\") in _win_norm_cmdline(cmdline)


def _win_process_snapshot():
    """Return the process fields needed for ancestry/identity ownership checks."""
    import psutil

    snapshot = []
    for proc in psutil.process_iter(["pid", "ppid", "cmdline", "create_time"]):
        try:
            info = dict(proc.info)
            if info.get("pid") is None or info.get("create_time") is None:
                continue
            snapshot.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return snapshot


def _win_is_supervisor(info: dict, kind: str) -> bool:
    cmdline = [str(arg) for arg in (info.get("cmdline") or [])]
    return bool(
        _win_cmdline_has_path(" ".join(cmdline), _RUN_SERVICE)
        and kind in {arg.lower() for arg in cmdline}
    ) or bool(
        is_frozen()
        and cmdline_has_mode(cmdline, SERVICE_MODE)
        and kind in {arg.lower() for arg in cmdline}
    )


def _win_is_service_owner(info: dict, kind: str) -> bool:
    args = [str(arg) for arg in (info.get("cmdline") or [])]
    cmdline = " ".join(args)
    if kind == "server":
        return _win_cmdline_has_path(
            cmdline, _REPO_ROOT / "osia" / "server" / "run_server.py",
        ) or (is_frozen() and cmdline_has_mode(args, SERVER_MODE))
    if kind == "tunnel":
        return _win_cmdline_has_path(cmdline, _TUNNEL_LOG)
    return False


def _win_collect_supervised_tree(kind: str,
                                 tree: _WinServiceTree | None = None
                                 ) -> _WinServiceTree:
    """Expand identities rooted at every matching old OSIA supervisor.

    Re-expansion after ``schtasks /end`` also catches a legacy supervisor child
    launched immediately before its parent exited: Windows retains the creator
    PID as ``ppid``, so it remains provably connected to the captured root.
    """
    tree = tree or _WinServiceTree()
    snapshot = _win_process_snapshot()
    for info in snapshot:
        if not _win_is_supervisor(info, kind):
            continue
        pid = int(info["pid"])
        tree.processes[pid] = float(info["create_time"])
        tree.supervisors.add(pid)

    changed = True
    while changed:
        changed = False
        for info in snapshot:
            pid = int(info["pid"])
            if pid in tree.processes:
                continue
            ppid = int(info.get("ppid") or 0)
            parent_created = tree.processes.get(ppid)
            if parent_created is None:
                continue
            created = float(info["create_time"])
            if created + 0.001 < parent_created:
                continue
            tree.processes[pid] = created
            changed = True
    return tree


def _win_process_for_identity(pid: int, created: float):
    """Return the same live process, or None after exit/PID reuse."""
    import psutil

    try:
        proc = psutil.Process(pid)
        if abs(float(proc.create_time()) - created) > 0.001:
            return None
        return proc
    except psutil.NoSuchProcess:
        return None


def _win_owned_alive_pids(tree: _WinServiceTree) -> set[int]:
    import psutil

    alive = set()
    for pid, created in tree.processes.items():
        try:
            if _win_process_for_identity(pid, created) is not None:
                alive.add(pid)
        except psutil.AccessDenied:
            alive.add(pid)  # inaccessible is not proof of exit: fail closed
    return alive


def _win_listener_pids(ports=_SERVER_PORTS) -> set[int]:
    import psutil

    listeners = set()
    wanted = set(ports)
    connections = psutil.net_connections(kind="tcp")
    for conn in connections:
        try:
            if str(conn.status).upper() != "LISTEN":
                continue
            port = getattr(conn.laddr, "port", None)
            if port is None and len(conn.laddr) >= 2:
                port = conn.laddr[1]
            if port in wanted:
                # An unattributed listener is still a conflict; the sentinel can
                # never match an owned PID and therefore fails closed as foreign.
                listeners.add(-1 if conn.pid is None else int(conn.pid))
        except (AttributeError, IndexError, TypeError):
            continue
    return listeners


def _win_unowned_service_pids(kind: str, tree: _WinServiceTree) -> set[int]:
    """Identify exact OSIA owners outside the captured tree; never kill them."""
    unowned = set()
    for info in _win_process_snapshot():
        pid = int(info["pid"])
        if pid not in tree.processes and _win_is_service_owner(info, kind):
            unowned.add(pid)
    return unowned


def _win_stop_barrier_state(task: str, kind: str,
                            tree: _WinServiceTree) -> tuple[bool, dict[str, int]]:
    _win_collect_supervised_tree(kind, tree)
    alive = _win_owned_alive_pids(tree)
    listener_pids = _win_listener_pids()
    old_listener_pids = listener_pids & set(tree.processes)
    foreign_listener_pids = listener_pids - set(tree.processes)
    unowned = _win_unowned_service_pids(kind, tree)
    state = {
        "task_running": int(_win_task_running_for_barrier(task)),
        "supervisors_alive": len(alive & tree.supervisors),
        "descendants_alive": len(alive - tree.supervisors),
        "old_listeners": len(old_listener_pids),
        "foreign_listeners": len(foreign_listener_pids),
        "unowned_owners": len(unowned),
    }
    return not any(state.values()), state


def _win_wait_stop_barrier(task: str, kind: str, tree: _WinServiceTree,
                           timeout: float) -> tuple[bool, dict[str, int]]:
    deadline = time.monotonic() + timeout
    last_state: dict[str, int] = {}
    while True:
        ready, last_state = _win_stop_barrier_state(task, kind, tree)
        if ready:
            return True, last_state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_state
        time.sleep(min(_WIN_STOP_POLL_SEC, remaining))


def _win_try_stop_barrier(task: str, kind: str, tree: _WinServiceTree,
                          timeout: float) -> tuple[bool, dict[str, int]]:
    """Convert a probe failure into fail-closed barrier state for escalation."""
    try:
        return _win_wait_stop_barrier(task, kind, tree, timeout)
    except Exception as exc:
        print(f"[win-service] stop barrier probe failed "
              f"category={type(exc).__name__}")
        return False, {"barrier_probe_error": 1}


def _win_terminate_identities(identities: dict[int, float], timeout: float) -> None:
    """Terminate only exact PID/create-time identities already proven owned."""
    import psutil

    targets = []
    for pid, created in identities.items():
        try:
            proc = _win_process_for_identity(pid, created)
            if proc is None:
                continue
            proc.terminate()
            targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not targets:
        return
    _gone, alive = psutil.wait_procs(targets, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if alive:
        psutil.wait_procs(alive, timeout=min(2.0, timeout))


def _win_terminate_child_jobs(kind: str, supervisor_pids: set[int]) -> bool:
    """Terminate members of the captured supervisors' named child jobs."""
    kernel32 = _kernel32()
    terminated = False
    for pid in supervisor_pids:
        handle = kernel32.OpenJobObjectW(
            _JOB_OBJECT_TERMINATE, False, _win_child_job_name(kind, pid),
        )
        if not handle:
            continue
        try:
            terminated = bool(kernel32.TerminateJobObject(handle, 1)) or terminated
        finally:
            kernel32.CloseHandle(handle)
    return terminated


def _win_force_stop_owned(task: str, kind: str,
                          tree: _WinServiceTree) -> None:
    """Escalate after graceful timeout without touching unrelated processes."""
    _win_end_task(task)
    _win_collect_supervised_tree(kind, tree)
    supervisors = {
        pid: tree.processes[pid]
        for pid in tree.supervisors if pid in tree.processes
    }
    _win_terminate_identities(supervisors, timeout=3.0)
    _win_collect_supervised_tree(kind, tree)
    _win_terminate_child_jobs(kind, set(tree.supervisors))
    _win_terminate_identities(dict(tree.processes), timeout=5.0)


def _win_coordinated_restart(kind: str) -> bool:
    """Signal, prove complete old-tree exit, and only then start one task."""
    task = _TASK_NAME[kind]
    lifecycle = None
    shutdown = None
    tree = _WinServiceTree()
    try:
        lifecycle = _WinNamedMutex(_win_lifecycle_mutex_name(kind))
        if not lifecycle.acquire(_WIN_LIFECYCLE_MUTEX_TIMEOUT):
            print("[win-service] restart failed category=lifecycle-mutex-timeout")
            return False
        try:
            _win_collect_supervised_tree(kind, tree)
            shutdown = _WinShutdownSignal(kind)
        finally:
            lifecycle.close()
            lifecycle = None

        stopped, state = _win_try_stop_barrier(
            task, kind, tree, _WIN_GRACEFUL_STOP_TIMEOUT,
        )
        if not stopped:
            _win_force_stop_owned(task, kind, tree)
            stopped, state = _win_try_stop_barrier(
                task, kind, tree, _WIN_FORCED_STOP_TIMEOUT,
            )
        if not stopped:
            details = ",".join(f"{key}={value}" for key, value in state.items())
            print(f"[win-service] restart failed category=stop-barrier-timeout {details}")
            return False

        # The old supervisors have closed their handles, so closing this final
        # signaled handle destroys the event. The replacement creates a fresh,
        # non-signaled event and cannot inherit stale shutdown state.
        shutdown.close()
        shutdown = None
        if not _win_task_registered(task) and not _win_register_task(kind):
            print("[win-service] restart failed category=task-registration")
            return False
        if not _win_run_task(task):
            print("[win-service] restart failed category=task-start")
            return False
        return True
    except Exception as exc:
        print(f"[win-service] restart failed category={type(exc).__name__}")
        return False
    finally:
        if lifecycle is not None:
            lifecycle.close()
        if shutdown is not None:
            shutdown.close()


def _win_coordinated_stop(kind: str) -> bool:
    """Stop one registered OSIA task without deleting or relaunching it."""

    task = _TASK_NAME[kind]
    lifecycle = None
    shutdown = None
    tree = _WinServiceTree()
    try:
        lifecycle = _WinNamedMutex(_win_lifecycle_mutex_name(kind))
        if not lifecycle.acquire(_WIN_LIFECYCLE_MUTEX_TIMEOUT):
            print("[win-service] stop failed category=lifecycle-mutex-timeout")
            return False
        try:
            _win_collect_supervised_tree(kind, tree)
            shutdown = _WinShutdownSignal(kind)
        finally:
            lifecycle.close()
            lifecycle = None

        stopped, state = _win_try_stop_barrier(
            task, kind, tree, _WIN_GRACEFUL_STOP_TIMEOUT,
        )
        if not stopped:
            _win_force_stop_owned(task, kind, tree)
            stopped, state = _win_try_stop_barrier(
                task, kind, tree, _WIN_FORCED_STOP_TIMEOUT,
            )
        if not stopped:
            details = ",".join(f"{key}={value}" for key, value in state.items())
            print(f"[win-service] stop failed category=stop-barrier-timeout {details}")
            return False
        return True
    except Exception as exc:
        print(f"[win-service] stop failed category={type(exc).__name__}")
        return False
    finally:
        if lifecycle is not None:
            lifecycle.close()
        if shutdown is not None:
            shutdown.close()


def _win_proc_running(image: str) -> bool:
    """True if a process with executable name ``image`` is running (tasklist)."""
    try:
        result = run_background_probe(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
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


def _win_osia_tunnel_running() -> bool:
    """True if OSIA's own cloudflared quick tunnel is running."""
    try:
        import psutil
    except Exception:
        return False
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() != "cloudflared.exe":
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if _win_cmdline_has_path(cmdline, _TUNNEL_LOG):
                return True
        except Exception:
            continue
    return False


def _win_pythonw() -> str:
    """Prefer the venv's windowless pythonw.exe; fall back to the running one."""
    if is_frozen():
        return str(_APP_EXE)
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


def _win_task_running_for_barrier(task: str) -> bool:
    """Task state probe that fails closed when Task Scheduler cannot be queried."""
    if not _win_task_registered(task):
        return False
    script = (
        "$ErrorActionPreference='Stop';"
        "try {"
        " $t = Get-ScheduledTask -TaskName $env:OSIA_TASK -ErrorAction Stop;"
        " if ($t.State -eq 'Running') { exit 0 } else { exit 1 }"
        "} catch { exit 2 }"
    )
    result = _ps(script, {"OSIA_TASK": task}, timeout=15.0)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    # A concurrent delete is also proof that no old task instance can be
    # Running; every other query failure leaves ownership uncertain.
    if not _win_task_registered(task):
        return False
    raise RuntimeError("scheduled-task state unavailable")


def _win_register_task(kind: str) -> bool:
    """Create/overwrite the per-user Scheduled Task for the server.

    AtLogOn trigger; runs the windowless ``run_service.py`` supervisor; no
    execution time limit, runs on battery, restart-on-failure — the closest
    Task Scheduler analogue to the Linux unit's ``Restart=always``. Needs no
    admin (LeastPrivilege / InteractiveToken for the current user). A branded
    name + description make it legible in Task Scheduler / "Startup apps".
    """
    task = _TASK_NAME.get(kind)
    if not task or kind != "server":
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
        executable = _win_pythonw()
        arguments = (
            f"{SERVICE_MODE} {kind}"
            if is_frozen()
            else f'"{_RUN_SERVICE}" {kind}'
        )
        working_dir = str(_APP_DIR if is_frozen() else _REPO_ROOT)
        r = _ps(script, {
            "OSIA_PYW": executable,
            "OSIA_ARG": arguments,
            "OSIA_REPO": working_dir,
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


def _win_register_wake_recovery_task() -> bool:
    """Create/overwrite the Windows resume recovery Scheduled Task.

    This task is intentionally separate from "OSIA Server": the server keeps
    its AtLogOn startup behavior, while this event trigger runs a small
    idempotent recovery script after Windows resumes from low power.
    """
    user = _win_current_user()
    subscription = (
        "<QueryList>"
        "<Query Id=\"0\" Path=\"System\">"
        "<Select Path=\"System\">"
        "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] "
        "and EventID=1]]"
        "</Select>"
        "</Query>"
        "</QueryList>"
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "$a = New-ScheduledTaskAction -Execute $env:OSIA_PYW"
        " -Argument $env:OSIA_ARG -WorkingDirectory $env:OSIA_REPO;"
        "$triggerClass = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler"
        " -ClassName MSFT_TaskEventTrigger;"
        "$t = New-CimInstance -CimClass $triggerClass -ClientOnly;"
        "$t.Enabled = $true;"
        "$t.Subscription = $env:OSIA_EVENT_SUBSCRIPTION;"
        "$t.Delay = 'PT30S';"
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries"
        " -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)"
        " -MultipleInstances IgnoreNew -StartWhenAvailable"
        " -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1);"
        "$p = New-ScheduledTaskPrincipal -UserId $env:OSIA_USER"
        " -LogonType Interactive -RunLevel Limited;"
        "Register-ScheduledTask -TaskName $env:OSIA_TASK -Action $a -Trigger $t"
        " -Settings $s -Principal $p -Description $env:OSIA_DESC -Force | Out-Null"
    )
    try:
        r = _ps(script, {
            "OSIA_PYW": _win_pythonw(),
            "OSIA_ARG": (
                WAKE_RECOVERY_MODE if is_frozen() else f'"{_WAKE_RECOVERY}"'
            ),
            "OSIA_REPO": str(_APP_DIR if is_frozen() else _REPO_ROOT),
            "OSIA_USER": user,
            "OSIA_TASK": _WAKE_TASK_NAME,
            "OSIA_DESC": _WAKE_TASK_DESC,
            "OSIA_EVENT_SUBSCRIPTION": subscription,
        })
    except Exception as e:
        print(f"[win-service] register {_WAKE_TASK_NAME} failed: {e}")
        return False
    if r.returncode != 0:
        print(f"[win-service] register {_WAKE_TASK_NAME} failed: {(r.stderr or r.stdout).strip()}")
    return r.returncode == 0


def _win_run_task(task: str) -> bool:
    try:
        return _schtasks(["/run", "/TN", task]).returncode == 0
    except Exception:
        return False


def _win_end_task(task: str) -> bool:
    """Request Task Scheduler to end its launched program; descendants are
    handled separately by OSIA's explicit ownership barrier."""
    try:
        result = _schtasks(["/end", "/TN", task])
    except Exception:
        return False
    return result.returncode == 0


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
        return _win_osia_tunnel_running()
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
        source_match = _win_cmdline_has_path(joined, _RUN_SERVICE) and kind in cmdline
        frozen_match = (
            is_frozen()
            and cmdline_has_mode(cmdline, SERVICE_MODE)
            and kind in cmdline
        )
        if source_match or frozen_match:
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
        raw_cmdline = proc.info.get("cmdline") or []
        if _win_cmdline_has_path(
            cmdline, _REPO_ROOT / "osia" / "server" / "run_server.py"
        ) or (is_frozen() and cmdline_has_mode(raw_cmdline, SERVER_MODE)):
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

    Targets ONLY OSIA's *quick* tunnel by cmdline — matched on the unique
    ``windows\\tunnel.log`` logfile path it always passes to cloudflared (the
    quick tunnel has no named-tunnel arg) — like ``_win_stop_server`` matches the
    server. Unrelated cloudflared instances the user runs for other projects are
    never collaterally killed. No-op when psutil is unavailable — the task's job
    object already reaped the child."""
    try:
        import psutil
    except Exception:
        return False
    killed = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() != "cloudflared.exe":
                continue
            if _win_cmdline_has_path(" ".join(proc.info.get("cmdline") or []), _TUNNEL_LOG):
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
    if kind == "tunnel":
        return False
    if is_frozen() and kind == "server":
        try:
            _SERVER_DISABLED_MARKER.unlink(missing_ok=True)
        except OSError:
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
    if is_frozen() and kind == "server":
        try:
            _SERVER_DISABLED_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _SERVER_DISABLED_MARKER.touch()
        except OSError:
            return False
    task = _TASK_NAME[kind]
    _win_end_task(task)      # stop the supervisor + its child (task job object)
    _win_delete_task(task)   # so it won't relaunch at the next login
    _win_kill_leftovers(kind)  # reap anything the job object didn't
    return True


# ── Public API (cross-platform) ─────────────────────────────────────────────

def install_windows_services() -> bool:
    """Register and start the OSIA server and Windows resume recovery tasks.

    Retires historical OSIA tunnel tasks/processes as an upgrade migration.
    Called by ``install.bat``.
    Windows-only; a harmless no-op (returns True) elsewhere.

    Returns True only if all Windows tasks registered successfully.
    """
    if not _IS_WINDOWS:
        return True
    # Migration-only cleanup: target the old OSIA task names and the exact
    # tunnel.log command-line signature, never personal cloudflared installs.
    _win_end_task(_TASK_NAME["tunnel"])
    _win_delete_task(_TASK_NAME["tunnel"])
    for legacy in _LEGACY_TASK_NAMES["tunnel"]:
        _win_delete_task(legacy)
    _win_kill_tunnel()
    if is_frozen() and _SERVER_DISABLED_MARKER.exists():
        _win_end_task(_TASK_NAME["server"])
        _win_delete_task(_TASK_NAME["server"])
        _win_delete_task(_WAKE_TASK_NAME)
        return True
    if not _win_register_task("server"):
        return False
    if _win_should_start("server"):
        _win_run_task(_TASK_NAME["server"])
    return _win_register_wake_recovery_task()


def remove_windows_services() -> bool:
    """Stop and unregister OSIA's Windows tasks without deleting user data."""
    if not _IS_WINDOWS:
        return True
    ok = True
    for kind in ("server", "tunnel"):
        task = _TASK_NAME[kind]
        if _win_task_registered(task):
            _win_end_task(task)
            ok = _win_delete_task(task) and ok
        _win_kill_leftovers(kind)
        for legacy in _LEGACY_TASK_NAMES.get(kind, ()):
            _win_delete_task(legacy)
    if _win_task_registered(_WAKE_TASK_NAME):
        _win_end_task(_WAKE_TASK_NAME)
        ok = _win_delete_task(_WAKE_TASK_NAME) and ok
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
    if kind == "tunnel":
        return False
    if is_frozen() and kind == "server" and _SERVER_DISABLED_MARKER.exists():
        return True
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
        if kind == "tunnel":
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
            return "running" if _win_osia_tunnel_running() else "stopped"
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


def stop_service(name: str) -> bool:
    """Stop the current service session without changing launch-at-login."""

    if _IS_WINDOWS:
        kind = _UNIT_KIND.get(name)
        if kind not in ("server", "tunnel"):
            return False
        return _win_coordinated_stop(kind)
    if sys.platform == "darwin":
        # The desktop does not own a separate system service on macOS.
        return True
    try:
        probe = _run(["systemctl", "is-active", name])
        state = (probe.stdout or "").strip()
        if state in {"inactive", "failed", "unknown", "deactivating"}:
            return True
        if probe.returncode == 4:  # systemd: unit does not exist
            return True
        result = _run(["pkexec", "systemctl", "stop", name], timeout=60.0)
    except FileNotFoundError:
        return True
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def restart_service(name: str) -> bool:
    """Restart the unit (without changing its enabled state). Returns True on
    success."""
    if _IS_WINDOWS:
        kind = _UNIT_KIND.get(name)
        if kind not in ("server", "tunnel"):
            return False
        if kind == "tunnel":
            return False
        mutex = None
        try:
            mutex = _WinNamedMutex(_win_restart_mutex_name(kind))
            if not mutex.acquire(_WIN_RESTART_MUTEX_TIMEOUT):
                print("[win-service] restart failed category=restart-mutex-timeout")
                return False
            if mutex.abandoned:
                print("[win-service] restart recovered category=abandoned-mutex")
            return _win_coordinated_restart(kind)
        except Exception as exc:
            print(f"[win-service] restart failed category={type(exc).__name__}")
            return False
        finally:
            if mutex is not None:
                mutex.close()
    try:
        result = _run(
            ["pkexec", "systemctl", "restart", name],
            timeout=60.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0
