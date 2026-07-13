"""Exclusive ownership for the single mobile application bridge.

The relay and LAN transports may both keep control/signalling sockets, but only
the holder of this lease may connect to the loopback OSIA WebSocket server.
When LAN-direct is enabled, the production coordinator also takes an OS-backed
interprocess lock so detached or manually launched processes cannot create a
second authoritative bridge.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Optional

from osia.config import CONFIG_DIR


_LOCK_FILE = Path(CONFIG_DIR) / "application_transport.lock"


class _InterprocessLock:
    """Non-blocking OS lock whose ownership dies with the owning process."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle = None
        self._file = None

    def try_acquire(self, transport: str) -> bool:
        if sys.platform == "win32":
            return self._try_acquire_windows()
        return self._try_acquire_posix(transport)

    def _try_acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        # The resolved lock path makes tests/repositories independent while the
        # Global namespace keeps all processes for this Windows user/session on
        # the same kernel mutex. The name contains no pairing material.
        digest = hashlib.sha256(
            str(self._lock_path.resolve()).casefold().encode("utf-8")
        ).hexdigest()[:24]
        name = f"Global\\OSIA.ApplicationTransport.{digest}"
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = create_mutex(None, True, name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(handle)
            return False
        self._handle = handle
        return True

    def _try_acquire_posix(self, transport: str) -> bool:
        import fcntl

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self._lock_path, "a+", encoding="utf-8")
        try:
            os.chmod(self._lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = {
                "pid": os.getpid(),
                "transport": transport,
                "acquired_at": int(time.time()),
            }
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(metadata, lock_file, separators=(",", ":"))
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except BlockingIOError:
            lock_file.close()
            return False
        except Exception:
            lock_file.close()
            raise
        self._file = lock_file
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            release_mutex = kernel32.ReleaseMutex
            release_mutex.argtypes = [wintypes.HANDLE]
            release_mutex.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            try:
                release_mutex(handle)
            finally:
                close_handle(handle)
        lock_file = self._file
        self._file = None
        if lock_file is not None:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


class ApplicationTransportLease:
    """A revocable, idempotently released application-transport lease."""

    def __init__(
        self,
        coordinator: "ApplicationTransportCoordinator",
        transport: str,
        owner: object,
        interprocess_lock: _InterprocessLock | None,
    ) -> None:
        self._coordinator = coordinator
        self._interprocess_lock = interprocess_lock
        self.transport = transport
        self.owner = owner
        self._released = False

    @property
    def active(self) -> bool:
        return not self._released and self._coordinator.is_owner(self)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._coordinator.release(self)

    def _release_interprocess(self) -> None:
        interprocess_lock = self._interprocess_lock
        self._interprocess_lock = None
        if interprocess_lock is not None:
            interprocess_lock.release()


class ApplicationTransportCoordinator:
    """Grant at most one application bridge lease across all transports."""

    def __init__(self, lock_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._lock_path = lock_path
        self._lease: Optional[ApplicationTransportLease] = None

    def try_acquire(
        self,
        transport: str,
        owner: object,
    ) -> Optional[ApplicationTransportLease]:
        if transport not in {"relay", "lan"}:
            raise ValueError("transport must be relay or lan")
        with self._lock:
            if self._lease is not None:
                return None
            interprocess_lock = None
            if self._lock_path is not None:
                interprocess_lock = _InterprocessLock(self._lock_path)
                if not interprocess_lock.try_acquire(transport):
                    return None
            try:
                lease = ApplicationTransportLease(
                    self,
                    transport,
                    owner,
                    interprocess_lock,
                )
            except Exception:
                if interprocess_lock is not None:
                    interprocess_lock.release()
                raise
            self._lease = lease
            return lease

    def is_owner(self, lease: ApplicationTransportLease) -> bool:
        with self._lock:
            return self._lease is lease

    def release(self, lease: ApplicationTransportLease) -> None:
        release_interprocess = False
        with self._lock:
            if self._lease is lease:
                self._lease = None
                release_interprocess = True
        if release_interprocess:
            lease._release_interprocess()

    @property
    def active_transport(self) -> Optional[str]:
        with self._lock:
            return self._lease.transport if self._lease is not None else None


# Production LAN and relay paths share this exact coordinator and OS lock.
# Test-local coordinators can omit lock_path to avoid touching user state.
APPLICATION_TRANSPORT_COORDINATOR = ApplicationTransportCoordinator(_LOCK_FILE)


class UncoordinatedApplicationTransportLease:
    """No-op lease used to keep the flag-off relay path non-contending."""

    def __init__(self, transport: str, owner: object) -> None:
        self.transport = transport
        self.owner = owner
        self._released = False

    @property
    def active(self) -> bool:
        return not self._released

    def release(self) -> None:
        self._released = True


def application_transport_coordination_enabled() -> bool:
    raw = os.getenv("OSIA_LAN_DIRECT_V1")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    from osia.config import load_settings

    return load_settings().get("lan_direct_v1") is True


def acquire_application_transport(transport: str, owner: object):
    """Acquire exclusively only when LAN-direct is explicitly enabled."""

    if not application_transport_coordination_enabled():
        return UncoordinatedApplicationTransportLease(transport, owner)
    return APPLICATION_TRANSPORT_COORDINATOR.try_acquire(transport, owner)
