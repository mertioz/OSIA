#!/usr/bin/env python3
"""Launch the OSIA Mobile WebSocket server via uvicorn."""
import sys
import os

# Ensure the project root is on sys.path so 'osia' is importable
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "osia.server.server:app",
        host="0.0.0.0",
        port=8765,
        log_level="info",
    )
