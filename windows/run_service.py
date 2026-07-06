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
_HANG_TIMEOUT = 75.0    # max time a server child may be alive-but-not-listening on
                        # 8765 before we treat it as HUNG and relaunch (WOR-47)

# No console window for the child console apps (python.exe / cloudflared.exe).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
    if kind == "server":
        # ``-u`` (unbuffered, exactly like the working console run
        # ``python -u run_server.py``) makes the child's uvicorn startup lines
        # and any crash traceback reach the child log *immediately* instead of
        # dying block-buffered in the child's stdio when it exits.
        return [_venv_python(), "-u", str(_RUN_SERVER)], str(_REPO)
    # tunnel — Cloudflare *quick* tunnel for http://127.0.0.1:8765 (identical
    # invocation to windows\start_tunnel.bat). The public trycloudflare.com URL
    # it prints to _TUNNEL_LOG is what osia.rendezvous publishes (WOR-64).
    #
    # WOR-67/WOR-16: ``--config NUL`` forces cloudflared to SKIP any
    # user-local ~/.cloudflared/config.yml. OSIA's Windows mobile path is this
    # quick tunnel + rendezvous publisher; if cloudflared accidentally reads a
    # fixed-host ingress config, the trycloudflare URL can 404 on every path. NUL
    # is the Windows null device (the Linux wrapper must use ``/dev/null`` here,
    # NOT NUL). Origin is 127.0.0.1 (not localhost) to force IPv4 — the server
    # binds 0.0.0.0:8765 which is IPv4-only, so a localhost→::1 resolution would
    # fail to connect.
    return (
        [str(_CLOUDFLARED), "tunnel", "--no-autoupdate", "--config", "NUL",
         "--logfile", str(_TUNNEL_LOG), "--url", "http://127.0.0.1:8765"],
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
                # Only OSIA's *own* quick tunnel counts as another owner — match
                # the unique logfile path we always pass (windows\tunnel.log),
                # NOT the bare image name (that would make us defer to any
                # foreign/stale cloudflared the user runs for something else,
                # forever). The quick tunnel has no named-tunnel arg, so the
                # logfile path is the OSIA-specific signature (mirrors
                # _win_kill_tunnel in osia/utils/systemd_manager.py).
                if (proc.info.get("name") or "").lower() == "cloudflared.exe" \
                        and _cmdline_has_path(cmdline, _TUNNEL_LOG):
                    return True
            else:
                if _cmdline_has_path(cmdline, _RUN_SERVER):
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


def main() -> int:
    kind = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if kind not in ("server", "tunnel"):
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

    if kind == "tunnel" and not _CLOUDFLARED.exists():
        _say(f"cloudflared.exe not found at {_CLOUDFLARED}; exiting.")
        return 1

    # WOR-64: on the tunnel branch only, start the rendezvous publisher — a
    # daemon thread that tails _TUNNEL_LOG for the quick tunnel's trycloudflare
    # URL and POSTs {proof, url} to the rendezvous Worker so the phone can find
    # this PC. Strictly best-effort and OUTSIDE the supervise loop below: it runs
    # in a daemon thread, never blocks process exit, and cannot touch the WOR-47
    # watchdog (which lives in the separate server process).
    if kind == "tunnel":
        try:
            sys.path.insert(0, str(_REPO))  # windows/ is on sys.path, not the repo root
            from osia.rendezvous import start_publisher
            start_publisher(_TUNNEL_LOG)
        except Exception as e:
            _say(f"rendezvous publisher not started (best-effort): {e!r}")

    cmd, cwd = _child_command(kind)
    _say(f"child command: {cmd}  (cwd={cwd})")
    my_child = None
    deferring = False  # only log the defer decision on transitions, not every poll
    launched_at = 0.0  # wall time of the current child's launch (for the hang check)
    healthy = False    # set once a server child is observed LISTENING on 8765
    while True:
        # Our child is still alive — keep watching it.
        if my_child is not None:
            rc = my_child.poll()
            if rc is None:
                # Alive is not the same as serving. The WOR-47 cold-boot hang
                # leaves the server worker blocked in its startup tail: poll()
                # reports it alive forever while 8765 never binds. Promote it to
                # "healthy" only once it actually listens; if it never does within
                # the hang window, kill it so the next iteration relaunches a fresh
                # one (its stack is in osia_server_boot.log via faulthandler).
                if kind == "server" and not healthy:
                    if _port_listening(8765):
                        healthy = True
                        _say(f"child pid={my_child.pid} is serving on 8765 "
                             f"({time.time() - launched_at:.1f}s after launch)")
                    elif time.time() - launched_at > _HANG_TIMEOUT:
                        _say(f"child pid={my_child.pid} alive but 8765 not "
                             f"listening after {_HANG_TIMEOUT:.0f}s — HUNG; killing "
                             f"for relaunch (stack in osia_server_boot.log)")
                        try:
                            my_child.kill()
                            # Reap the corpse before relaunching, and deliberately
                            # KEEP my_child set (do not null it here): the next loop
                            # iteration then runs the normal exited-child path, which
                            # captures the real dead_pid and EXCLUDES it from the
                            # _other_owner_running defer check. Otherwise the just-
                            # killed child (TerminateProcess is not an immediate reap)
                            # could still be visible to psutil and match its own
                            # "run_server.py" cmdline, making the supervisor defer to
                            # its own corpse instead of relaunching — delaying the
                            # self-heal this watchdog exists to deliver.
                            my_child.wait(timeout=10)
                        except Exception as e:
                            _say(f"kill of hung child failed: {e!r}")
                        time.sleep(_RESTART_SEC)
                        continue
                time.sleep(_POLL_SEC)
                continue
            _say(f"child pid={my_child.pid} exited rc={rc}{_decode_rc(rc)} "
                 f"(see {child_log_path.name} for its output)")
        dead_pid = my_child.pid if my_child is not None else -1
        my_child = None
        # Did a detached replacement (restart_server) or another instance take
        # over? If so, defer to it instead of racing for the port.
        if _other_owner_running(kind, exclude_pid=dead_pid):
            if not deferring:
                _say(f"another {kind} owner already running (pid!={dead_pid}); "
                     f"deferring instead of racing for the port")
                deferring = True
            time.sleep(_POLL_SEC)
            continue
        deferring = False
        # Nobody is serving — (re)launch the child.
        try:
            my_child = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=(child_log or subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
                # COPY os.environ (never replace it — a bare dict would drop PATH
                # and break Windows DLL resolution at interpreter startup).
                # PYTHONFAULTHANDLER arms faulthandler from the first instruction
                # (belt-and-suspenders with run_server's own _init_boot_diag);
                # PYTHONUNBUFFERED matches the `-u` flag so the child log keeps
                # its tail even if the child is killed.
                env={**os.environ, "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1"},
            )
            launched_at = time.time()
            healthy = False
            _say(f"launched {kind} child pid={my_child.pid}")
        except Exception as e:
            _say(f"failed to launch {kind}: {e!r}")
            time.sleep(_RESTART_SEC)
            continue
        # Give it a head start, then if it died immediately (e.g. the port was
        # still held during a restart) back off so we don't hot-loop.
        time.sleep(_STARTUP_GRACE)
        rc = my_child.poll()
        if rc is not None:
            _say(f"child pid={my_child.pid} died within {_STARTUP_GRACE}s "
                 f"rc={rc}{_decode_rc(rc)} (see {child_log_path.name})")
            time.sleep(_RESTART_SEC)


if __name__ == "__main__":
    sys.exit(main())
