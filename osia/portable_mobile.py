"""Own the companion process launched by a Linux or macOS portable desktop."""
import subprocess
import threading

from osia.config import get_setting, set_setting

_lock = threading.RLock()
_command = None
_child = None


def initialize(command):
    global _command
    with _lock:
        _command = tuple(command)
        if enabled():
            start()


def managed():
    return _command is not None


def enabled():
    return get_setting("portable_mobile_startup", True) is True


def running():
    with _lock:
        return _child is not None and _child.poll() is None


def start():
    global _child
    with _lock:
        if not running():
            try:
                _child = subprocess.Popen(_command)
            except OSError:
                return False
        return True


def stop():
    global _child
    with _lock:
        if running():
            _child.terminate()
            try:
                _child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _child.kill()
                _child.wait()
        _child = None
        return True


def set_enabled(value):
    with _lock:
        if value and not start():
            return False
        if not value:
            stop()
        set_setting("portable_mobile_startup", value)
        return True
