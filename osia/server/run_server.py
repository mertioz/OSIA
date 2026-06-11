#!/usr/bin/env python3
"""Launch the OSIA Mobile WebSocket server via uvicorn."""
import sys
import os

# Ensure the project root is on sys.path so 'osia' is importable
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# --- Boot diagnostics (WOR-47) ---------------------------------------------
# The mobile server has been seen to HANG (not crash) in the startup tail at
# cold-boot logon: the worker blocks before its first log line, port 8765 never
# binds, and nothing is logged because the *inherited* stdout handle may itself
# be blocked. So write boot breadcrumbs to a DEDICATED log with its OWN file
# handle (independent of that inherited stdout) and arm faulthandler to dump
# every thread's stack after 45s — pinpointing where the hang sits. Stdlib only,
# fully best-effort: diagnostics must never themselves break startup.
import faulthandler
import tempfile
import time

# Dedicated boot-diagnostics log handle; kept alive process-wide so its fileno
# stays valid for faulthandler (never GC-closed) for the life of the process.
_BOOT_DIAG = None


def _boot_ts() -> str:
    """Local wall-clock timestamp (ms precision) for the boot-log breadcrumbs."""
    t = time.time()
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
            + f".{int((t % 1) * 1000):03d}")


def _init_boot_diag():
    """Open the dedicated boot-diagnostics log, write a banner, and arm
    faulthandler (live + a one-shot 45s stack dump). Returns the open handle
    (also stored in the module global ``_BOOT_DIAG`` so it is never closed)."""
    global _BOOT_DIAG
    try:
        f = open(
            os.path.join(tempfile.gettempdir(), "osia_server_boot.log"),
            "a", encoding="utf-8", errors="replace", buffering=1,
        )
        f.write(f"\n==== boot {_boot_ts()} pid={os.getpid()} "
                f"interp={sys.executable!r} ====\n")
        f.flush()
        try:
            # Live faulthandler (file=f) so a hard crash dumps here too, plus a
            # one-shot watchdog: if we are still inside the startup tail 45s from
            # now, dump every thread's stack — that is the hung worker's frame.
            faulthandler.enable(file=f, all_threads=True)
            faulthandler.dump_traceback_later(45, repeat=False, file=f)
        except Exception:
            pass
        _BOOT_DIAG = f
        return f
    except Exception:
        return None


def _boot_mark(msg: str) -> None:
    """Write a flushed, timestamped breadcrumb to the dedicated boot log only —
    never via the (possibly blocked) inherited stdout. No-op if the log is absent."""
    f = _BOOT_DIAG
    if f is None:
        return
    try:
        f.write(f"{_boot_ts()} [boot] {msg}\n")
        f.flush()
    except Exception:
        pass


_init_boot_diag()
_boot_mark("interpreter up")

if __name__ == "__main__":
    # Windows consoles default to cp1252, which raises UnicodeEncodeError the
    # moment a log line contains a non-ASCII char (e.g. the "▶" in
    # "[OSIA Agent] ▶ tool:") — that crash propagates to the mobile client as an
    # error instead of the AI's reply and blocks persisting new conversations.
    # Force UTF-8 (replace undecodable chars) on the std streams. Guarded for the
    # detached-launch case (Task Scheduler / pythonw), where stdout/stderr may be
    # None or lack .reconfigure — this must never crash startup. Linux/Mac
    # consoles are UTF-8 already, so the whole block is win32-only.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                if _stream is not None and hasattr(_stream, "reconfigure"):
                    _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        # Belt-and-suspenders: child processes (the restart re-spawn, browser
        # tools) inherit a UTF-8 default too. setdefault never clobbers an
        # explicit override the environment may already carry.
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # Windows windowless safety net: under pythonw.exe (e.g. the detached
    # restart_server re-spawn in osia/server/server.py, which does not redirect
    # streams) sys.stdout/sys.stderr are None, and uvicorn's first log write
    # would then raise and kill the server at ~1s. When a stream is missing,
    # point it at a temp-dir log so the server starts cleanly regardless of how
    # it was launched. No-op when a parent already provided real/redirected
    # streams (the run_service.py supervisor, or a console run).
    if sys.platform == "win32" and (sys.stdout is None or sys.stderr is None):
        try:
            import tempfile
            _log = open(
                os.path.join(tempfile.gettempdir(), "osia_server.log"),
                "a", encoding="utf-8", errors="replace",
            )
            if sys.stdout is None:
                sys.stdout = _log
            if sys.stderr is None:
                sys.stderr = _log
        except Exception:
            pass

    # First-launch config bootstrap: create settings.json + the mobile pairing
    # token (if missing) BEFORE importing/starting the server, so the server
    # authenticates against a persisted token. On Linux/Mac this server process
    # is the single token creator; on Windows the GUI already created it first,
    # so this is an idempotent no-op for the token there. Best-effort.
    _boot_mark("before ensure_config_files")
    try:
        from osia.config import ensure_config_files
        ensure_config_files()
    except Exception as e:
        print(f"[CONFIG] ensure_config_files skipped: {e}")
    _boot_mark("after ensure_config_files")

    # Port guard (WOR-47): if 8765 is already held — e.g. the supervisor
    # relaunched us while a detached restart_server replacement still owns the
    # port, or an external owner is serving — uvicorn would die on bind and the
    # supervisor would treat that as a crash to retry. Probe with a throwaway
    # socket and instead exit *cleanly* (rc=0) so the supervisor's defer logic
    # handles it. Mirror uvicorn/asyncio's own bind options so the probe trips on
    # a *live* listener, not a port merely left in TIME_WAIT.
    import socket
    _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            # POSIX asyncio binds with SO_REUSEADDR; match it so a TIME_WAIT port
            # (which uvicorn *would* bind) is not misread as "in use". On Windows
            # SO_REUSEADDR means "hijack an active socket", so we omit it there.
            _probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _probe.bind(("0.0.0.0", 8765))
    except OSError:
        _boot_mark("port 8765 already in use; exiting cleanly")
        print("port 8765 already in use; exiting cleanly")
        sys.exit(0)
    finally:
        _probe.close()

    _boot_mark("before uvicorn.run / app import")
    import uvicorn
    uvicorn.run(
        "osia.server.server:app",
        host="0.0.0.0",
        port=8765,
        log_level="info",
    )
