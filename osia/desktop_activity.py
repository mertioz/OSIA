"""Presentation-only facts about native input, shared by GUI and server processes.

No arguments, screenshots or conversation data are recorded. Failures here must
never change dispatch, its return value, cancellation or ActionTruth.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import json
import hashlib
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


def mouse_feedback_enabled(*, conversation_id: str = "") -> bool:
    """Decorate mouse-enabled chats only; keyboard/UIA execution stays independent."""
    try:
        from osia.slash_commands import current_chat_commands

        if current_chat_commands()[0]:
            return False
        if conversation_id:
            from osia.conversations import conversation_special_mode

            return not conversation_special_mode(conversation_id)
        return True
    except Exception:
        return False  # unavailable presentation must never block native input


@contextmanager
def native_input_activity(*, enabled: bool | None = None):
    global _depth
    if not (mouse_feedback_enabled() if enabled is None else enabled):
        yield
        return
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


DESKTOP_INTENT_TOOLS = frozenset({
    "desktop_click", "desktop_type", "desktop_press", "desktop_scroll",
    "activate_desktop_surface",
})


def _intent_path(turn_id: str) -> Path:
    key = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    return activity_directory() / f"intent-{key}.json"


def _finished_intent_path(path: Path) -> Path:
    return path.parent / "finished" / (path.stem + ".done")


def observe_desktop_intent(turn_id: str, tool_name: str, *, conversation_id: str = "") -> None:
    """A requested native action is an intention fact, not an execution permit."""
    if (
        not turn_id or tool_name not in DESKTOP_INTENT_TOOLS
        or not mouse_feedback_enabled(conversation_id=conversation_id)
    ):
        return
    try:
        import psutil

        path = _intent_path(turn_id)
        if _finished_intent_path(path).exists():
            return  # stale presentation notification, never an execution veto
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "process_started": psutil.Process().create_time(),
                                         "active": True, "changed_at": time.time()}), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        pass


def finish_desktop_intent(turn_id: str) -> None:
    """Only the matching terminal turn clears its visual phase, across processes."""
    if turn_id:
        try:
            path = _intent_path(turn_id)
            finished = _finished_intent_path(path)
            finished.parent.mkdir(parents=True, exist_ok=True)
            finished.touch(exist_ok=True)
            path.unlink(missing_ok=True)
        except Exception:
            pass


def desktop_activity_visible(*, now: float | None = None) -> bool:
    """Observe only live processes; PID reuse and abandoned files aren't activity."""
    try:
        import psutil

        current = time.time() if now is None else now
        for path in activity_directory().glob("*.json"):
            try:
                if path.name.startswith("intent-") and _finished_intent_path(path).exists():
                    continue
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
