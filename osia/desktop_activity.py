"""Presentation-only facts about native input, shared by GUI and server processes.

No arguments, screenshots or conversation data are recorded. Failures here must
never change dispatch, its return value, cancellation or ActionTruth.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import threading
import time

VISUAL_HOLD_SECONDS = 0.7
_lock = threading.RLock()
_depth = 0


def activity_directory() -> Path:
    from osia import config

    return Path(config.CONFIG_DIR) / "runtime" / "desktop-feedback"


def _publish(active: bool) -> None:
    try:
        import psutil

        directory = activity_directory()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{os.getpid()}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "pid": os.getpid(),
            "process_started": psutil.Process().create_time(),
            "active": active,
            "changed_at": time.time(),
        }), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        pass


@contextmanager
def native_input_activity():
    global _depth
    with _lock:
        _depth += 1
        if _depth == 1:
            _publish(True)
    try:
        yield
    finally:
        with _lock:
            _depth -= 1
            if _depth == 0:
                _publish(False)


def shows_native_input(function):
    @wraps(function)
    def observed(*args, **kwargs):
        with native_input_activity():
            return function(*args, **kwargs)
    return observed


def desktop_activity_visible(*, now: float | None = None) -> bool:
    """Observe only live processes; PID reuse and abandoned files aren't activity."""
    try:
        import psutil

        current = time.time() if now is None else now
        for path in activity_directory().glob("*.json"):
            try:
                if path.stat().st_size > 512:
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    continue
                if value.get("active") is not True and not (
                    value.get("active") is False
                    and 0 <= current - float(value["changed_at"]) < VISUAL_HOLD_SECONDS
                ):
                    continue
                process = psutil.Process(int(value["pid"]))
                if abs(process.create_time() - float(value["process_started"])) < 0.01:
                    return True
            except (OSError, ValueError, TypeError, KeyError, psutil.Error):
                continue
    except Exception:
        pass
    return False
