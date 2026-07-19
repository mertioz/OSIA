#!/usr/bin/env python3
"""Recover OSIA's Windows server and tunnel after system resume.

Launched by the "OSIA Wake Recovery" Scheduled Task. The script deliberately
uses the existing service helpers so it preserves the normal Task Scheduler
startup model and the targeted OSIA cleanup rules.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_SOURCE_ROOT = Path(__file__).resolve().parent.parent
if (_SOURCE_ROOT / "osia").is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from osia.runtime import SERVER_MODE, cmdline_has_mode, is_frozen, resource_root

_REPO = resource_root()
_HERE = _REPO / "windows"
_TUNNEL_LOG = _HERE / "tunnel.log"
_LOG_PATH = Path(tempfile.gettempdir()) / "osia_wake_recovery.log"
_LOCK_PATH = Path(tempfile.gettempdir()) / "osia_wake_recovery.lock"

_SERVER_UNIT = "osia-server.service"
_TUNNEL_UNIT = "osia-tunnel.service"
_SERVER_PORT = 8765
_TUNNEL_ORIGIN = "http://127.0.0.1:8765"


def _ts() -> str:
    t = time.time()
    return (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        + f".{int((t % 1) * 1000):03d}"
    )


def _log(fh, msg: str) -> None:
    try:
        fh.write(f"{_ts()} [wake_recovery] {msg}\n")
        fh.flush()
    except Exception:
        pass


def _norm(value: str) -> str:
    return (value or "").lower().replace("/", "\\")


def _cmdline_has_path(cmdline: str, path: Path) -> bool:
    return str(path).lower().replace("/", "\\") in _norm(cmdline)


def _clip(value: str, limit: int = 700) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


@contextmanager
def _single_instance(log_fh) -> Iterator[bool]:
    lock_fh = None
    acquired = False
    try:
        lock_fh = open(_LOCK_PATH, "a+", encoding="utf-8", errors="replace")
        if sys.platform == "win32":
            import msvcrt

            lock_fh.seek(0)
            try:
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                _log(log_fh, f"lock busy: {_LOCK_PATH} ({e})")
                yield False
                return
            acquired = True
            lock_fh.seek(0)
            lock_fh.truncate()
            lock_fh.write(f"pid={os.getpid()} started={_ts()}\n")
            lock_fh.flush()
        else:
            acquired = True
        _log(log_fh, f"lock acquired: {_LOCK_PATH}")
        yield True
    except Exception as e:
        _log(log_fh, f"lock acquisition failed: {e!r}")
        yield False
    finally:
        if acquired and lock_fh is not None and sys.platform == "win32":
            try:
                import msvcrt

                lock_fh.seek(0)
                msvcrt.locking(lock_fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception as e:
                _log(log_fh, f"lock release warning: {e!r}")
        if lock_fh is not None:
            try:
                lock_fh.close()
            except Exception:
                pass
        if acquired:
            _log(log_fh, "lock released")


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _process_cmdline(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        import psutil

        return " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return ""


def _port_owner(port: int) -> str:
    listeners = _port_listeners(port)
    if listeners:
        pid = listeners[0]
        return f"pid={pid} cmdline={_clip(_process_cmdline(pid))}"
    return "no LISTEN owner found"


def _port_listeners(port: int) -> list[int]:
    try:
        import psutil
    except Exception as e:
        return []
    listeners: list[int] = []
    try:
        for conn in psutil.net_connections(kind="tcp"):
            try:
                if conn.status != psutil.CONN_LISTEN:
                    continue
                if getattr(conn.laddr, "port", None) != port:
                    continue
                if conn.pid is not None:
                    listeners.append(int(conn.pid))
            except Exception:
                continue
    except Exception:
        return []
    return listeners


def _find_server_children() -> list[tuple[int, str]]:
    try:
        import psutil
    except Exception:
        return []
    matches: list[tuple[int, str]] = []
    run_server = _REPO / "osia" / "server" / "run_server.py"
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            raw_cmdline = proc.info.get("cmdline") or []
            cmdline = " ".join(raw_cmdline)
            if _cmdline_has_path(cmdline, run_server) or (
                is_frozen() and cmdline_has_mode(raw_cmdline, SERVER_MODE)
            ):
                matches.append((int(proc.info["pid"]), cmdline))
        except Exception:
            continue
    return matches


def _find_osia_cloudflared() -> list[tuple[int, str]]:
    try:
        import psutil
    except Exception:
        return []
    matches: list[tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() != "cloudflared.exe":
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            compact = " ".join(cmdline.split()).lower()
            if not _cmdline_has_path(cmdline, _TUNNEL_LOG):
                continue
            if f"--url {_TUNNEL_ORIGIN}".lower() not in compact:
                continue
            matches.append((int(proc.info["pid"]), cmdline))
        except Exception:
            continue
    return matches


def _wait_for_port(log_fh, port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_listening(port):
            _log(log_fh, f"port {port} listening: {_port_owner(port)}")
            return True
        time.sleep(2.0)
    _log(log_fh, f"port {port} timeout after {timeout:.0f}s: {_port_owner(port)}")
    return False


def _wait_for_cloudflared(log_fh, timeout: float = 60.0) -> list[tuple[int, str]]:
    deadline = time.time() + timeout
    last_count = -1
    matches: list[tuple[int, str]] = []
    while time.time() < deadline:
        matches = _find_osia_cloudflared()
        if len(matches) == 1:
            pid, cmdline = matches[0]
            _log(log_fh, f"cloudflared match: pid={pid} cmdline={_clip(cmdline)}")
            return matches
        if len(matches) != last_count:
            _log(log_fh, f"cloudflared matches while waiting: count={len(matches)}")
            last_count = len(matches)
        time.sleep(2.0)
    if matches:
        for pid, cmdline in matches:
            _log(log_fh, f"cloudflared duplicate/stale match: pid={pid} cmdline={_clip(cmdline)}")
    else:
        _log(log_fh, "cloudflared timeout: no OSIA quick tunnel process matched")
    return matches


def _restart(log_fh, restart_service, unit: str, label: str) -> bool:
    try:
        ok = bool(restart_service(unit))
    except Exception as e:
        _log(log_fh, f"{label} restart raised: {e!r}")
        return False
    _log(log_fh, f"{label} restart result: {'ok' if ok else 'failed'}")
    return ok


def main() -> int:
    with open(_LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1) as log_fh:
        _log(log_fh, f"==== wake recovery start pid={os.getpid()} ====")
        _log(log_fh, f"repo={_REPO}")
        if sys.platform != "win32":
            _log(log_fh, f"non-Windows platform {sys.platform!r}; no-op")
            return 0

        with _single_instance(log_fh) as locked:
            if not locked:
                _log(log_fh, "another wake recovery is already running; exiting")
                return 0

            try:
                if str(_REPO) not in sys.path:
                    sys.path.insert(0, str(_REPO))
                from osia.utils.systemd_manager import restart_service
            except Exception as e:
                _log(log_fh, f"failed to import service helpers: {e!r}")
                return 1

            server_restart_ok = _restart(log_fh, restart_service, _SERVER_UNIT, "server")
            port_ok = _wait_for_port(log_fh, _SERVER_PORT)
            server_children = _find_server_children()
            _log(log_fh, f"server child matches: count={len(server_children)}")
            for pid, cmdline in server_children:
                _log(log_fh, f"server child match: pid={pid} cmdline={_clip(cmdline)}")
            listeners = _port_listeners(_SERVER_PORT)
            _log(log_fh, f"port {_SERVER_PORT} listener pids: {listeners}")
            if not port_ok:
                _log(log_fh, "failed: server port did not recover")
                return 1
            if len(set(listeners)) > 1:
                _log(log_fh, "failed: duplicate server port listeners detected")
                return 1

            if is_frozen():
                if not server_restart_ok:
                    _log(log_fh, "failed: server scheduled-task restart command failed")
                    return 1
                _log(log_fh, "wake recovery completed successfully")
                return 0

            tunnel_restart_ok = _restart(log_fh, restart_service, _TUNNEL_UNIT, "tunnel")
            tunnel_matches = _wait_for_cloudflared(log_fh)
            if len(tunnel_matches) != 1:
                _log(log_fh, "failed: expected exactly one OSIA cloudflared process")
                return 1

            if not server_restart_ok or not tunnel_restart_ok:
                _log(log_fh, "failed: a scheduled task restart command failed")
                return 1
            _log(log_fh, "wake recovery completed successfully")
            return 0


if __name__ == "__main__":
    sys.exit(main())
