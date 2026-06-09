#!/usr/bin/env python3
"""Launch the OSIA Mobile WebSocket server via uvicorn."""
import sys
import os

# Ensure the project root is on sys.path so 'osia' is importable
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

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
    try:
        from osia.config import ensure_config_files
        ensure_config_files()
    except Exception as e:
        print(f"[CONFIG] ensure_config_files skipped: {e}")

    import uvicorn
    uvicorn.run(
        "osia.server.server:app",
        host="0.0.0.0",
        port=8765,
        log_level="info",
    )
