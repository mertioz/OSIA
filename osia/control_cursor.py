"""Temporary Windows cursor ownership in a small, isolated helper process.

The inherited pipe closes on GUI exit/crash. The helper owns copies of the
original cursors and restores them on EOF, timeout, error and ordinary stop.
No registry or cursor-scheme preference is written. Other platforms are no-ops.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time

CURSOR_HELPER_MODE = "--osia-control-cursor"
CURSOR_IDS = {"arrow": 32512, "hand": 32649}


def validate_cursor_bytes(data: bytes) -> bytes:
    if len(data) < 62 or len(data) > 131072:
        raise ValueError("invalid cursor size")
    reserved, kind, count = struct.unpack_from("<HHH", data)
    width, height, _, _, hot_x, hot_y, size, offset = struct.unpack_from("<BBBBHHII", data, 6)
    if (reserved, kind, count, offset) != (0, 2, 1, 22):
        raise ValueError("invalid cursor header")
    header, bitmap_w, bitmap_h, planes, bits = struct.unpack_from("<IiiHH", data, 22)
    compression, bitmap_size = struct.unpack_from("<II", data, 38)
    expected = 40 + width * height * 4 + ((width + 31) // 32) * 4 * height
    if not (
        8 <= width <= 128 and 8 <= height <= 128
        and hot_x < width and hot_y < height
        and (header, bitmap_w, bitmap_h, planes, bits) == (40, width, height * 2, 1, 32)
        and compression == 0 and bitmap_size == width * height * 4
        and size == expected and len(data) == offset + size
    ):
        raise ValueError("invalid cursor bitmap")
    return data


class WindowsCursorAPI:
    def __init__(self):
        from ctypes import wintypes as w

        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.LoadCursorW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
        self.user.LoadCursorW.restype = w.HANDLE
        self.user.CopyImage.argtypes = [w.HANDLE, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        self.user.CopyImage.restype = w.HANDLE
        self.user.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
        self.user.LoadImageW.restype = w.HANDLE
        self.user.SetSystemCursor.argtypes = [w.HANDLE, w.DWORD]
        self.user.SetSystemCursor.restype = w.BOOL
        self.user.DestroyCursor.argtypes = [w.HANDLE]
        self.user.DestroyCursor.restype = w.BOOL

    def copy_original(self, cursor_id):
        shared = self.user.LoadCursorW(None, ctypes.c_void_p(cursor_id))
        copy = self.user.CopyImage(shared, 2, 0, 0, 0) if shared else None
        if not copy:
            raise OSError("cursor backup unavailable")
        return copy

    def load(self, path):
        handle = self.user.LoadImageW(None, str(path), 2, 0, 0, 0x10)
        if not handle:
            raise OSError("cursor bitmap unavailable")
        return handle

    def replace(self, handle, cursor_id):
        # SetSystemCursor consumes the owned handle on success.
        if not self.user.SetSystemCursor(handle, cursor_id):
            self.user.DestroyCursor(handle)
            raise OSError("cursor replacement unavailable")

    def destroy(self, handle):
        self.user.DestroyCursor(handle)


class CursorSession:
    def __init__(self, api):
        self.api = api
        self.originals = {}

    def apply(self, cursors, directory):
        data = {name: validate_cursor_bytes(base64.b64decode(cursors[name], validate=True))
                for name in CURSOR_IDS}
        if not self.originals:
            try:
                for name, cursor_id in CURSOR_IDS.items():
                    self.originals[name] = self.api.copy_original(cursor_id)
            except Exception:
                for handle in self.originals.values():
                    self.api.destroy(handle)
                self.originals.clear()
                raise
        try:
            for name, cursor_id in CURSOR_IDS.items():
                path = Path(directory) / f"{name}.cur"
                path.write_bytes(data[name])
                self.api.replace(self.api.load(path), cursor_id)
        except Exception:
            self.restore()
            raise

    def restore(self):
        # Attempt both even if the first one fails. Never reset the user's
        # entire scheme or replace text/resize/busy cursors with an arrow.
        originals, self.originals = self.originals, {}
        for name, handle in originals.items():
            try:
                self.api.replace(handle, CURSOR_IDS[name])
            except Exception:
                pass


def run_helper(stream=None, *, api=None) -> int:
    if sys.platform != "win32" and api is None:
        return 0
    # Windowed PyInstaller/pythonw deliberately sets sys.stdin to None, even
    # when CreateProcess supplied a pipe. Recover that exact inherited handle.
    if stream is None:
        stream = getattr(sys.stdin, "buffer", None)
        if stream is None:
            import msvcrt
            from ctypes import wintypes as w

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetStdHandle.argtypes = [w.DWORD]
            kernel.GetStdHandle.restype = w.HANDLE
            handle = kernel.GetStdHandle(w.DWORD(-10 & 0xffffffff))
            if not handle or handle == ctypes.c_void_p(-1).value:
                return 1
            stream = os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")
    mutex = None
    if api is None:
        from ctypes import wintypes as w

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
        kernel.CreateMutexW.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.ReleaseMutex.argtypes = [w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        mutex = kernel.CreateMutexW(None, False, "Local\\OSIA.DesktopControlCursor")
        if not mutex:
            return 1
        if kernel.WaitForSingleObject(mutex, 0) not in (0, 0x80):
            kernel.CloseHandle(mutex)
            return 0  # another GUI helper owns the original cursors
    messages = queue.Queue()

    def read_pipe():
        try:
            while line := stream.readline(262145):
                if len(line) > 262144:
                    break
                messages.put(json.loads(line))
        except Exception:
            pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_pipe, daemon=True)
    reader.start()
    session = None
    last_message = time.monotonic()
    try:
        session = CursorSession(api or WindowsCursorAPI())
        with tempfile.TemporaryDirectory(prefix="osia-control-cursor-") as directory:
            while True:
                try:
                    message = messages.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - last_message > 2:
                        session.restore()
                    continue
                if message is None:
                    break
                last_message = time.monotonic()
                if message.get("active") is True:
                    if message.get("cursors"):
                        session.apply(message["cursors"], directory)
                else:
                    session.restore()
    except Exception:
        return 1
    finally:
        if session is not None:
            session.restore()
        if mutex is not None:
            kernel.ReleaseMutex(mutex)
            kernel.CloseHandle(mutex)
    return 0


class CursorClient:
    """Pipe I/O and process startup stay off Qt's UI thread."""
    def __init__(self):
        self.messages = queue.Queue()
        self.thread = None
        self.closed = False

    def set_active(self, active, cursors=None):
        if self.closed or sys.platform != "win32":
            return
        if self.thread is None or not self.thread.is_alive():
            if not active:
                return
            self.messages = queue.Queue()
            self.thread = threading.Thread(target=self._worker, name="osia-cursor-feedback", daemon=True)
            self.thread.start()
        self.messages.put({"active": bool(active), "cursors": cursors})

    def close(self):
        self.closed = True
        self.messages.put(None)

    def _worker(self):
        from osia.runtime import is_frozen

        process = None
        active = False
        try:
            while True:
                try:
                    message = self.messages.get(timeout=0.5)
                except queue.Empty:
                    if not active:
                        continue
                    message = {"active": True}  # helper liveness only
                if message is None:
                    break
                active = message["active"]
                if process is None:
                    if not active:
                        continue
                    command = ([sys.executable, CURSOR_HELPER_MODE] if is_frozen()
                               else [sys.executable, "-m", "osia.control_cursor"])
                    process = subprocess.Popen(
                        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True,
                        cwd=str(Path(__file__).resolve().parent.parent),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
                process.stdin.flush()
        except Exception:
            pass
        finally:
            if process is not None and process.stdin:
                try:
                    process.stdin.close()  # EOF restores on graceful shutdown too
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(run_helper())
