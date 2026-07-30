"""Controlled one-instance relaunch coordination for the OSIA desktop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from osia.config import CONFIG_DIR
from osia.runtime import (
    RELAUNCH_MODE,
    application_dir,
    application_executable,
    is_frozen,
)
from osia.utils.subprocess_policy import ProcessMode, process_kwargs


DESKTOP_MUTEX_NAME = "Local\\OSIA.Desktop"
RELAUNCH_MUTEX_NAME = "Local\\OSIA.Relaunch"
RELAUNCH_MARKER = Path(CONFIG_DIR) / "restart_pending.json"
PARENT_EXIT_TIMEOUT = 90.0
MUTEX_RELEASE_TIMEOUT = 30.0

_request_lock = threading.Lock()
_request_pending = False
_desktop_mutex_lock = threading.Lock()
_desktop_mutex_owned = None


def coordinator_command(parent_pid: int, *, frozen: bool | None = None) -> list[str]:
    """Return the locally-generated detached coordinator command."""
    frozen = is_frozen() if frozen is None else bool(frozen)
    if frozen:
        return [str(application_executable()), RELAUNCH_MODE, str(int(parent_pid))]
    return [
        str(application_executable()),
        "-m",
        "osia.relaunch",
        "--coordinator",
        str(int(parent_pid)),
    ]


def desktop_command(*, frozen: bool | None = None) -> list[str]:
    """Return the exact command for one fresh interactive desktop."""
    frozen = is_frozen() if frozen is None else bool(frozen)
    if frozen:
        return [str(application_executable()), "--wake"]
    return [str(application_executable()), "-m", "osia.main", "--wake"]


def _detached_kwargs() -> dict:
    kwargs = {
        "cwd": str(application_dir()),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    try:
        log_path = Path(CONFIG_DIR) / "logs" / "relaunch.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_stream = log_path.open("a", encoding="utf-8", errors="replace")
        kwargs["stdout"] = log_stream
        kwargs["stderr"] = log_stream
    except OSError:
        # Inherit the current diagnostic handles if the OSIA log is unavailable.
        pass
    if sys.platform == "win32":
        kwargs = process_kwargs(ProcessMode.RELAUNCH_COORDINATOR, kwargs)
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _close_child_streams(kwargs: dict) -> None:
    streams = {kwargs.get("stdout"), kwargs.get("stderr")}
    for stream in streams:
        if hasattr(stream, "close"):
            try:
                stream.close()
            except OSError:
                pass


def _write_marker(parent_pid: int) -> None:
    RELAUNCH_MARKER.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "parent_pid": int(parent_pid),
        "coordinator_pid": os.getpid(),
        "created": int(time.time()),
    }
    temp = RELAUNCH_MARKER.with_suffix(".tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    temp.replace(RELAUNCH_MARKER)


def clear_stale_restart_state() -> None:
    """Remove the non-sensitive marker; mutexes remain the authority."""
    try:
        RELAUNCH_MARKER.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_relaunch(*, parent_pid: int | None = None, process_factory=None) -> None:
    """Start one detached coordinator or raise before the desktop quits."""
    global _request_pending
    with _request_lock:
        if _request_pending:
            raise RuntimeError("restart_already_pending")
        _request_pending = True
    pid = int(parent_pid or os.getpid())
    factory = process_factory or subprocess.Popen
    try:
        _write_marker(pid)
        kwargs = _detached_kwargs()
        try:
            factory(coordinator_command(pid), **kwargs)
        finally:
            _close_child_streams(kwargs)
    except Exception:
        clear_stale_restart_state()
        with _request_lock:
            _request_pending = False
        raise


def _wait_for_parent_exit(parent_pid: int, timeout: float) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_object_0 = 0
        wait_timeout = 258
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(synchronize, False, int(parent_pid))
        if not handle:
            return True
        try:
            result = kernel32.WaitForSingleObject(handle, max(0, round(timeout * 1000)))
            return result == wait_object_0
        finally:
            kernel32.CloseHandle(handle)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(int(parent_pid), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.1)
    return False


def _acquire_windows_mutex(name: str, timeout: float, *, initial_owner: bool):
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    error_already_exists = 183
    wait_object_0 = 0
    wait_abandoned = 0x80
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, bool(initial_owner), name)
    if not handle:
        return False
    if initial_owner and ctypes.get_last_error() == error_already_exists:
        kernel32.CloseHandle(handle)
        return False
    if not initial_owner:
        result = kernel32.WaitForSingleObject(handle, max(0, round(timeout * 1000)))
        if result not in (wait_object_0, wait_abandoned):
            kernel32.CloseHandle(handle)
            return False
    return kernel32, handle


def _release_windows_mutex(owned) -> None:
    if not owned:
        return
    kernel32, handle = owned
    try:
        kernel32.ReleaseMutex(handle)
    finally:
        kernel32.CloseHandle(handle)


def acquire_desktop_instance_mutex():
    """Acquire the one desktop mutex for both source and packaged entry points."""

    global _desktop_mutex_owned
    if sys.platform != "win32":
        return None
    with _desktop_mutex_lock:
        if _desktop_mutex_owned is not None:
            return _desktop_mutex_owned
        owned = _acquire_windows_mutex(
            DESKTOP_MUTEX_NAME, 0.0, initial_owner=True,
        )
        if owned is False:
            return False
        _desktop_mutex_owned = owned
        return owned


def run_coordinator(
    parent_pid: int,
    *,
    wait_parent=_wait_for_parent_exit,
    wait_desktop_mutex=None,
    process_factory=None,
) -> int:
    """Wait for graceful shutdown/mutex release, then launch exactly once."""
    coordinator_mutex = _acquire_windows_mutex(
        RELAUNCH_MUTEX_NAME, 0.0, initial_owner=True,
    )
    if sys.platform == "win32" and coordinator_mutex is False:
        return 2
    factory = process_factory or subprocess.Popen
    try:
        _write_marker(parent_pid)
        if not wait_parent(int(parent_pid), PARENT_EXIT_TIMEOUT):
            return 3
        if wait_desktop_mutex is None:
            desktop_mutex = _acquire_windows_mutex(
                DESKTOP_MUTEX_NAME, MUTEX_RELEASE_TIMEOUT, initial_owner=False,
            )
            if sys.platform == "win32" and desktop_mutex is False:
                return 4
            _release_windows_mutex(desktop_mutex)
        elif not wait_desktop_mutex(MUTEX_RELEASE_TIMEOUT):
            return 4
        kwargs = _detached_kwargs()
        try:
            factory(desktop_command(), **kwargs)
        finally:
            _close_child_streams(kwargs)
        return 0
    except Exception:
        return 5
    finally:
        clear_stale_restart_state()
        _release_windows_mutex(coordinator_mutex)


def main(args: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if args is None else args)
    if len(values) != 2 or values[0] != "--coordinator":
        return 2
    try:
        parent_pid = int(values[1])
    except (TypeError, ValueError):
        return 2
    if parent_pid <= 0:
        return 2
    return run_coordinator(parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
