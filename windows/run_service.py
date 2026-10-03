#!/usr/bin/env python3
"""Windows-only supervisor for OSIA's background mobile server.

This is the Windows equivalent of the Linux ``osia-server.service`` systemd
unit. It is launched by the per-user Scheduled Task ("OSIA Server") that
``install.bat`` /
``osia.utils.systemd_manager.install_windows_services`` register with an AtLogOn
trigger. As a result the services:

  * start at login, **before** (and independently of) the OSIA GUI,
  * keep running when the GUI is closed, and
  * are restarted automatically by this loop if the child crashes — mirroring
    systemd's ``Restart=always`` / ``RestartSec=3`` (Task Scheduler's own
    restart-on-failure has a one-minute minimum, far slower than systemd).

Usage::

    pythonw run_service.py server

Why a loop instead of relying on Task Scheduler restart-on-failure: besides the
faster restart, the loop stays compatible with the phone-triggered
``restart_server`` WebSocket command (``osia/server/server.py``), which spawns a
**detached** replacement server process and exits. When our child exits and we
detect another owner of the service already (re)starting, we defer to it instead
of racing it for port 8765.

Windowless by construction: launched via ``pythonw.exe`` and every child is
spawned with ``CREATE_NO_WINDOW`` so nothing flashes a console at login. The
supervisor's own (timestamped, line-buffered) lines go to
``osia_{kind}_service.log`` and each child's stdout+stderr to
``osia_{kind}_child.log`` — both in the OS temp dir — so a silent pythonw
failure is still diagnosable after the fact.
"""
import os
import sys
import time
import socket
import json
import tempfile
import subprocess
import ctypes
from ctypes import wintypes
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
if (_SOURCE_ROOT / "osia").is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from osia.runtime import (
    SERVER_MODE,
    application_dir,
    application_executable,
    cmdline_has_mode,
    is_frozen,
    resource_root,
    source_tree_fingerprint,
)
from osia.utils.subprocess_policy import ProcessMode, process_kwargs

# windows/run_service.py → <repo>/windows → <repo>
_REPO = resource_root()
_RUN_SERVER = _REPO / "osia" / "server" / "run_server.py"

_RESTART_SEC = 3.0      # delay before relaunching a crashed child (systemd RestartSec)
_POLL_SEC = 2.0         # cadence for re-checking a live child / an external owner
_STARTUP_GRACE = 2.0    # head start before re-checking a freshly launched child
_HANG_TIMEOUT = 270.0   # bounded grace for canonical startup recovery before relaunch
                        # 8765 before we treat it as HUNG and relaunch (WOR-47)
_CHILD_STOP_TIMEOUT = 8.0
_LIFECYCLE_MUTEX_TIMEOUT = 5.0

_WIN_WAIT_OBJECT_0 = 0x00000000
_WIN_WAIT_ABANDONED = 0x00000080
_WIN_WAIT_TIMEOUT = 0x00000102
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_KERNEL32 = None


def _kernel32():
    """Configured kernel32 handle API (loaded lazily for import-safe tests)."""
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    if sys.platform != "win32":
        raise OSError("Windows lifecycle primitives are unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32 = kernel32
    return kernel32


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _shutdown_event_name(kind: str) -> str:
    return f"Local\\OSIA.SupervisorShutdown.{kind}"


def _lifecycle_mutex_name(kind: str) -> str:
    return f"Local\\OSIA.SupervisorLifecycle.{kind}"


def _child_job_name(kind: str, supervisor_pid: int | None = None) -> str:
    pid = os.getpid() if supervisor_pid is None else supervisor_pid
    return f"Local\\OSIA.SupervisorChildren.{kind}.{pid}"


class _ShutdownEvent:
    """Manual-reset event used by service control to request a clean stop."""

    def __init__(self, kind: str):
        self._kernel32 = _kernel32()
        self._handle = self._kernel32.CreateEventW(
            None, True, False, _shutdown_event_name(kind),
        )
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateEventW failed")

    def is_set(self) -> bool:
        return self._kernel32.WaitForSingleObject(
            self._handle, 0,
        ) == _WIN_WAIT_OBJECT_0

    def wait(self, timeout: float) -> bool:
        millis = max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = self._kernel32.WaitForSingleObject(self._handle, millis)
        if result == _WIN_WAIT_OBJECT_0:
            return True
        if result == _WIN_WAIT_TIMEOUT:
            return False
        raise OSError(ctypes.get_last_error(), "shutdown event wait failed")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _NamedMutex:
    """Small named-mutex wrapper; abandoned ownership is safely recoverable."""

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


class _ChildJob:
    """Own the supervised child tree and reap it if the supervisor disappears."""

    def __init__(self, kind: str):
        self._kernel32 = _kernel32()
        self._handle = self._kernel32.CreateJobObjectW(None, _child_job_name(kind))
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, child) -> bool:
        process_handle = getattr(child, "_handle", None)
        if process_handle is None:
            return False
        return bool(self._kernel32.AssignProcessToJobObject(
            self._handle, wintypes.HANDLE(int(process_handle)),
        ))

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _norm_cmdline(cmdline: str) -> str:
    return (cmdline or "").lower().replace("/", "\\")


def _cmdline_has_path(cmdline: str, path: Path) -> bool:
    return str(path).lower().replace("/", "\\") in _norm_cmdline(cmdline)


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


def _ts() -> str:
    """Local wall-clock timestamp with millisecond precision for log prefixes."""
    t = time.time()
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            + f".{int((t % 1) * 1000):03d}")


def _say(msg: str) -> None:
    """Write one timestamped supervisor line to the (line-buffered) log and flush.

    Flushing every line is what makes the supervisor diagnosable under
    ``pythonw.exe``: a block-buffered stream loses its tail when the process is
    killed (e.g. at logoff or when the child takes the port down with it), which
    is exactly why earlier failures were silent.
    """
    try:
        print(f"{_ts()} [run_service] {msg}")
        sys.stdout.flush()
    except Exception:
        pass


def _open_log(path: Path, line_buffered: bool):
    """Append-open ``path`` as UTF-8 text; never raises (returns None on error).

    ``line_buffered=True`` (``buffering=1``) flushes the supervisor's own log on
    every newline so nothing is lost if the process dies. The child log is left
    at default buffering — the child runs under ``python -u`` and owns its own
    flushing — so the two writers never fight over one buffer.
    """
    try:
        return open(
            path, "a", encoding="utf-8", errors="replace",
            buffering=1 if line_buffered else -1,
        )
    except Exception:
        return None


def _child_command(kind: str):
    """(argv, cwd) for the supervised child."""
    if kind != "server":
        raise ValueError("unsupported service kind")
    if is_frozen():
        return [str(application_executable()), SERVER_MODE], str(application_dir())
    # ``-u`` (unbuffered, exactly like the working console run
    # ``python -u run_server.py``) makes the child's uvicorn startup lines
    # and any crash traceback reach the child log *immediately* instead of
    # dying block-buffered in the child's stdio when it exits.
    return [_venv_python(), "-u", str(_RUN_SERVER)], str(_REPO)


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
            raw_cmdline = proc.info.get("cmdline") or []
            cmdline = " ".join(raw_cmdline)
            if _cmdline_has_path(cmdline, _RUN_SERVER) or (
                cmdline_has_mode(raw_cmdline, SERVER_MODE)
            ):
                return True
        except Exception:
            continue
    return False


# Common Windows NTSTATUS exit codes seen when a child dies during process/DLL
# init — the usual face of a *crash* (vs. the WOR-47 *hang*, which leaves the
# child alive). Mapped so the supervisor log names them instead of printing a
# bare 0xC000xxxx the reader then has to look up.
_NTSTATUS = {
    0xC0000135: "STATUS_DLL_NOT_FOUND",
    0xC0000142: "STATUS_DLL_INIT_FAILED",
    0xC0000005: "STATUS_ACCESS_VIOLATION",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN",
    0xC000007B: "STATUS_INVALID_IMAGE_FORMAT",
}


def _decode_rc(rc) -> str:
    """Annotate a child exit code with its Windows NTSTATUS name when it is one
    of the common startup-crash codes. Popen may surface these as a large
    unsigned value or its signed-32-bit twin, so normalise via ``& 0xFFFFFFFF``
    before lookup. Returns '' (nothing to add) for clean or unrecognised codes."""
    try:
        code = rc & 0xFFFFFFFF
    except Exception:
        return ""
    name = _NTSTATUS.get(code)
    return f" ({name} 0x{code:08X})" if name else ""


def _port_listening(port: int) -> bool:
    """True if something accepts a TCP connection on 127.0.0.1:port right now.

    Distinguishes a *serving* server child from one that is alive but HUNG — the
    WOR-47 cold-boot symptom where the worker blocks in the startup tail, 8765
    never binds, yet poll() keeps reporting the process alive. The short timeout
    keeps the supervisor's poll loop responsive."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _server_health_ready() -> bool:
    """Accept only the health response for this supervisor's runtime source."""
    try:
        with urlopen("http://127.0.0.1:8765/health", timeout=1.5) as response:
            if int(response.status) != 200:
                return False
            payload = json.loads(response.read(32_768).decode("utf-8"))
    except (HTTPError, OSError, ValueError, TimeoutError):
        return False
    if not isinstance(payload, dict) or payload.get("status") != "healthy":
        return False
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        return False
    expected_mode = "frozen" if is_frozen() else "source"
    expected_root = str(application_dir().resolve()).casefold()
    matches = bool(
        runtime.get("source_mode") == expected_mode
        and str(runtime.get("checkout_root") or "").casefold() == expected_root
        and str(runtime.get("source_fingerprint") or "")
        == source_tree_fingerprint()
    )
    if expected_mode == "source":
        expected_entrypoint = str(
            (_REPO / "osia" / "server" / "server.py").resolve()
        ).casefold()
        matches = matches and (
            str(runtime.get("entrypoint") or "").casefold()
            == expected_entrypoint
        )
    return matches


def _stop_child(child) -> None:
    """Stop and reap one supervised child without allowing a relaunch."""
    if child is None:
        return
    try:
        if child.poll() is not None:
            return
        _say(f"shutdown: terminating child pid={child.pid}")
        child.terminate()
        child.wait(timeout=_CHILD_STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        _say(f"shutdown: child pid={child.pid} timed out; forcing exit")
        try:
            child.kill()
            child.wait(timeout=_CHILD_STOP_TIMEOUT)
        except Exception as exc:
            _say(f"shutdown: forced child exit failed category={type(exc).__name__}")
    except Exception as exc:
        _say(f"shutdown: child stop failed category={type(exc).__name__}")


def _supervise(kind: str, cmd, cwd: str, child_log, child_log_path: Path,
               shutdown, lifecycle, child_job) -> int:
    """Run the crash-restart loop until the named shutdown event is set."""
    my_child = None
    deferring = False
    launched_at = 0.0
    healthy = False
    try:
        while not shutdown.is_set():
            if my_child is not None:
                rc = my_child.poll()
                if rc is None:
                    if kind == "server" and not healthy:
                        if _server_health_ready():
                            healthy = True
                            _say(f"child pid={my_child.pid} passed canonical /health on 8765 "
                                 f"({time.time() - launched_at:.1f}s after launch)")
                        elif time.time() - launched_at > _HANG_TIMEOUT:
                            _say(f"child pid={my_child.pid} alive but 8765 not "
                                 f"listening after {_HANG_TIMEOUT:.0f}s — HUNG; killing "
                                 f"for relaunch (stack in osia_server_boot.log)")
                            try:
                                my_child.kill()
                                my_child.wait(timeout=10)
                            except Exception as exc:
                                _say(f"hung-child kill failed category={type(exc).__name__}")
                            if shutdown.wait(_RESTART_SEC):
                                break
                            continue
                    if shutdown.wait(_POLL_SEC):
                        break
                    continue
                _say(f"child pid={my_child.pid} exited rc={rc}{_decode_rc(rc)} "
                     f"(see {child_log_path.name} for its output)")
            dead_pid = my_child.pid if my_child is not None else -1
            my_child = None

            if shutdown.is_set():
                break
            if _other_owner_running(kind, exclude_pid=dead_pid):
                if not deferring:
                    _say(f"another {kind} owner already running (pid!={dead_pid}); "
                         f"deferring instead of racing for the port")
                    deferring = True
                if shutdown.wait(_POLL_SEC):
                    break
                continue
            if kind == "server" and _port_listening(8765):
                if not deferring:
                    _say("port 8765 is occupied by an unattributed listener; "
                         "deferring without starting or terminating it")
                    deferring = True
                if shutdown.wait(_POLL_SEC):
                    break
                continue
            deferring = False

            # Serialize the final event check with the controller's SetEvent.
            # This closes the last check/Popen race during an expected shutdown.
            if not lifecycle.acquire(_LIFECYCLE_MUTEX_TIMEOUT):
                _say("launch coordination timed out; retrying")
                if shutdown.wait(_RESTART_SEC):
                    break
                continue
            try:
                if shutdown.is_set():
                    break
                try:
                    child_kwargs = process_kwargs(
                        ProcessMode.BACKGROUND_SUPERVISED,
                        {
                            "cwd": cwd,
                            "stdout": (child_log or subprocess.DEVNULL),
                            "stderr": subprocess.STDOUT,
                            "env": {
                                **os.environ,
                                "PYTHONFAULTHANDLER": "1",
                                "PYTHONUNBUFFERED": "1",
                            },
                        },
                    )
                    my_child = subprocess.Popen(cmd, **child_kwargs)
                    try:
                        assigned = child_job.assign(my_child)
                    except Exception as exc:
                        _say(f"child ownership failed pid={my_child.pid} "
                             f"category={type(exc).__name__}; stopping")
                        _stop_child(my_child)
                        return 1
                    if not assigned:
                        _say(f"child ownership failed pid={my_child.pid}; stopping")
                        _stop_child(my_child)
                        return 1
                    launched_at = time.time()
                    healthy = False
                    _say(f"launched {kind} child pid={my_child.pid}")
                except Exception as exc:
                    _say(f"failed to launch {kind} category={type(exc).__name__}")
                    my_child = None
            finally:
                lifecycle.release()

            if my_child is None:
                if shutdown.wait(_RESTART_SEC):
                    break
                continue

            if shutdown.wait(_STARTUP_GRACE):
                break
            rc = my_child.poll()
            if rc is not None:
                _say(f"child pid={my_child.pid} died within {_STARTUP_GRACE}s "
                     f"rc={rc}{_decode_rc(rc)} (see {child_log_path.name})")
                if shutdown.wait(_RESTART_SEC):
                    break
        _say("shutdown requested; supervision stopped")
        return 0
    finally:
        _stop_child(my_child)


def main(args: list[str] | None = None) -> int:
    values = sys.argv[1:] if args is None else args
    kind = (values[0] if values else "").strip().lower()
    if kind != "server":
        return 2

    # Two separate logs in the OS temp dir: the supervisor's own timestamped
    # lines, and the child's stdout+stderr verbatim. Keeping them apart means a
    # child traceback is never interleaved with — or lost behind — the
    # supervisor's buffering, and each is readable on its own.
    log_dir = Path(tempfile.gettempdir())
    log_path = log_dir / f"osia_{kind}_service.log"
    child_log_path = log_dir / f"osia_{kind}_child.log"

    # pythonw.exe has no real stdout/stderr; redirect ours so any print is safe
    # and the supervisor stays diagnosable. Line-buffered so nothing is lost.
    log = _open_log(log_path, line_buffered=True)
    if log is not None:
        try:
            sys.stdout = log
            sys.stderr = log
        except Exception:
            pass
    child_log = _open_log(child_log_path, line_buffered=False)

    _say(f"==== supervisor start: kind={kind} pid={os.getpid()} "
         f"interp={sys.executable!r} ====")
    _say(f"supervisor log: {log_path}")
    _say(f"child log:      {child_log_path}"
         + ("" if child_log is not None else "  (FAILED TO OPEN — child output lost)"))

    cmd, cwd = _child_command(kind)
    shutdown = None
    lifecycle = None
    child_job = None
    try:
        shutdown = _ShutdownEvent(kind)
        lifecycle = _NamedMutex(_lifecycle_mutex_name(kind))
        child_job = _ChildJob(kind)
        return _supervise(
            kind, cmd, cwd, child_log, child_log_path,
            shutdown, lifecycle, child_job,
        )
    except Exception as exc:
        _say(f"supervisor lifecycle setup failed category={type(exc).__name__}")
        return 1
    finally:
        if child_job is not None:
            child_job.close()
        if lifecycle is not None:
            lifecycle.close()
        if shutdown is not None:
            shutdown.close()


if __name__ == "__main__":
    sys.exit(main())
