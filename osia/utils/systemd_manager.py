"""Service control for OSIA's GUI "Startup" toggles.

Linux: a thin wrapper around ``systemctl``. Privileged operations
(enable/disable) are routed through ``pkexec`` so they work from a desktop
session without a controlling terminal. Read-only queries (``is-enabled``,
``is-active``) don't need elevation.

Windows (WOR-11): there is no systemd. The two units the GUI knows about map to
native Windows control paths instead:

  * ``osia-tunnel.service`` -> the ``windows\\*_tunnel.bat`` scripts
    (cloudflared.exe as a hidden background process).
  * ``osia-server.service`` -> the OSIA mobile server (``run_server.py``)
    launched/terminated as a detached background process.

The public function names/signatures are unchanged across platforms so the GUI
(``osia/gui/main_popup.py``) and every other caller work without modification.
"""

import subprocess
import sys
from pathlib import Path

# ── Windows detection + mappings ────────────────────────────────────────────
_IS_WINDOWS = sys.platform == "win32"

# windows/ control scripts live at <repo>/windows. This file is at
# <repo>/osia/utils/systemd_manager.py, so three parents up is the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WINDOWS_DIR = _REPO_ROOT / "windows"

# Local port the OSIA mobile server listens on (see osia/server/run_server.py).
_SERVER_PORT = 8765

# Map the systemd unit names the GUI uses to a logical kind so the Windows
# implementation can pick the right control path.
_UNIT_KIND = {
    "osia-tunnel.service": "tunnel",
    "osia-server.service": "server",
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

def _win_run_script(script: str) -> bool:
    """Run a ``windows\\<script>`` control batch file. True on exit code 0."""
    path = _WINDOWS_DIR / script
    if not path.exists():
        print(f"[win-service] control script not found: {path}")
        return False
    try:
        # .bat files must be run via cmd.exe, not executed directly.
        result = subprocess.run(
            ["cmd", "/c", str(path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except Exception as e:
        print(f"[win-service] {script} failed: {e}")
        return False
    return result.returncode == 0


def _win_proc_running(image: str) -> bool:
    """True if a process with executable name ``image`` is running (tasklist)."""
    try:
        result = subprocess.run(
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


def _win_python() -> str:
    """Prefer the venv's windowless pythonw.exe; fall back to the running one."""
    pythonw = _REPO_ROOT / "venv" / "Scripts" / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else (sys.executable or "pythonw.exe")


def _win_start_server() -> bool:
    """Launch the OSIA mobile server (run_server.py) as a detached process.

    The server runs under the windowless ``pythonw.exe`` (no console flashes),
    which means it has NO real stdout/stderr — ``sys.stdout``/``sys.stderr`` are
    ``None``. The server writes on startup (uvicorn's logging plus torch/HF
    import warnings), so without redirecting those streams the very first write
    raises and the process dies at ~1s, never binding port 8765 — the bug behind
    "Serveur mobile: éteint" on Windows. Redirect both to a log file in the temp
    dir so the server starts cleanly AND its output stays diagnosable; fall back
    to DEVNULL if the log can't be opened. (WOR-11 Windows server fix.)
    """
    import tempfile
    run_server = _REPO_ROOT / "osia" / "server" / "run_server.py"
    log_target = subprocess.DEVNULL
    try:
        log_target = open(
            Path(tempfile.gettempdir()) / "osia_server.log",
            "a", encoding="utf-8", errors="replace",
        )
    except Exception:
        log_target = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [_win_python(), str(run_server)],
            cwd=str(_REPO_ROOT),
            stdout=log_target,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        return True
    except Exception as e:
        print(f"[win-service] failed to start server: {e}")
        return False
    finally:
        # The child inherited its own copy of the handle; close the parent's so
        # we don't leak a file handle for the GUI process's lifetime.
        if log_target is not subprocess.DEVNULL:
            try:
                log_target.close()
            except Exception:
                pass


def _win_stop_server() -> bool:
    """Terminate the running OSIA mobile server process(es), waiting for them to
    actually exit so a follow-up start (restart_service) finds port 8765 free."""
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


def _win_enable(name: str) -> bool:
    kind = _UNIT_KIND.get(name)
    if kind == "tunnel":
        return _win_run_script("start_tunnel.bat")
    if kind == "server":
        return _win_start_server()
    print(f"[win-service] unknown unit: {name}")
    return False


def _win_disable(name: str) -> bool:
    kind = _UNIT_KIND.get(name)
    if kind == "tunnel":
        return _win_run_script("stop_tunnel.bat")
    if kind == "server":
        return _win_stop_server()
    print(f"[win-service] unknown unit: {name}")
    return False


# ── Public API (cross-platform) ─────────────────────────────────────────────

def is_service_enabled(name: str) -> bool:
    """True if the unit is enabled (will start at boot).

    Windows has no boot-unit concept for these, so it mirrors "currently
    running" — the GUI toggle then reflects reality (on = running).
    """
    if _IS_WINDOWS:
        return get_service_status(name) == "running"
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
        # No single "restart" primitive for the Windows control paths — stop
        # then start. Success is defined by the start step.
        _win_disable(name)
        return _win_enable(name)
    try:
        result = _run(
            ["pkexec", "systemctl", "restart", name],
            timeout=60.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return result.returncode == 0
