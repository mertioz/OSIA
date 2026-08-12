"""Dedicated console Native Messaging host; stdout is protocol-only."""

from __future__ import annotations

import os
import struct
import sys
import threading
import time

from osia.personal_browser.protocol import (
    MAX_ENVELOPE_BYTES,
    ProtocolError,
    decode_message,
    encode_message,
    read_framed,
    write_framed,
)
from osia.personal_browser.registration import origin_allowed


def _diagnostic(code: str) -> None:
    try:
        sys.stderr.write(f"[OSIA NativeHost] {code[:64]}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _connect_pipe(timeout_seconds: float = 8.0):
    if sys.platform != "win32":
        raise OSError("unsupported_platform")
    import win32con
    import win32file
    import win32pipe

    from osia.personal_browser.registration_windows import pipe_name

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            return win32file.CreateFile(
                pipe_name(),
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0, None, win32con.OPEN_EXISTING, 0, None,
            )
        except Exception:
            if time.monotonic() >= deadline:
                raise OSError("broker_unavailable")
            try:
                win32pipe.WaitNamedPipe(pipe_name(), 250)
            except Exception:
                time.sleep(0.05)


def _pipe_write(handle, message: dict, lock: threading.Lock) -> None:
    import win32file

    payload = encode_message(message)
    with lock:
        win32file.WriteFile(handle, struct.pack("<I", len(payload)) + payload)


def _pipe_read_exact(handle, size: int) -> bytes | None:
    import win32file

    output = []
    while size:
        _code, data = win32file.ReadFile(handle, size)
        if not data:
            return None
        output.append(data)
        size -= len(data)
    return b"".join(output)


def _pipe_read(handle) -> dict | None:
    header = _pipe_read_exact(handle, 4)
    if header is None:
        return None
    size = struct.unpack("<I", header)[0]
    if size <= 0 or size > MAX_ENVELOPE_BYTES:
        raise ProtocolError("message_too_large")
    payload = _pipe_read_exact(handle, size)
    return decode_message(payload) if payload is not None else None


def _pipe_has_data(handle) -> bool:
    import win32pipe

    _data, available, _remaining = win32pipe.PeekNamedPipe(handle, 0)
    return bool(available)


def run(origin: str) -> int:
    if not origin_allowed(origin):
        _diagnostic("origin_not_allowed")
        return 2
    try:
        pipe = _connect_pipe()
    except OSError:
        _diagnostic("broker_unavailable")
        return 3
    output_lock = threading.Lock()
    pipe_lock = threading.Lock()
    stopped = threading.Event()

    def broker_to_browser() -> None:
        try:
            while not stopped.is_set():
                if not _pipe_has_data(pipe):
                    stopped.wait(0.01)
                    continue
                message = _pipe_read(pipe)
                if message is None:
                    break
                with output_lock:
                    write_framed(sys.stdout.buffer, message, native_endian=True)
        except Exception:
            _diagnostic("broker_channel_closed")
        finally:
            stopped.set()
            os._exit(0)

    reader = threading.Thread(target=broker_to_browser, daemon=True)
    try:
        first_message = read_framed(sys.stdin.buffer, native_endian=True)
        if first_message is None:
            return 0
        if (
            first_message.get("message_type") != "hello"
            or first_message.get("caller_origin") != origin
        ):
            raise ProtocolError("origin_not_allowed")
        _pipe_write(pipe, first_message, pipe_lock)
        reader.start()
        while not stopped.is_set():
            message = read_framed(sys.stdin.buffer, native_endian=True)
            if message is None:
                break
            _pipe_write(pipe, message, pipe_lock)
    except ProtocolError as exc:
        _diagnostic(exc.code)
        return 4
    except Exception:
        _diagnostic("browser_channel_closed")
    finally:
        stopped.set()
        try:
            import win32file

            win32file.CloseHandle(pipe)
        except Exception:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values == ["--self-test"]:
        return 0 if sys.platform == "win32" else 2
    if not values:
        _diagnostic("missing_origin")
        return 2
    return run(str(values[0]))


if __name__ == "__main__":
    raise SystemExit(main())
