#!/usr/bin/env python3
"""Launch the OSIA Mobile WebSocket server via uvicorn."""
import sys
import os

# Ensure the project root is on sys.path so 'osia' is importable
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

if __name__ == "__main__":
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
