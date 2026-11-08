#!/usr/bin/env python3
"""Launch the OSIA Mobile WebSocket server via uvicorn."""

import faulthandler
import os
import sys
import tempfile
import time


# Ensure the project root is on sys.path so 'osia' is importable.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)


# Boot diagnostics (WOR-47). The dedicated handle remains alive process-wide so
# faulthandler never depends on a possibly blocked inherited stdout handle.
_BOOT_DIAG = None


def _boot_ts() -> str:
    """Local wall-clock timestamp with millisecond precision."""

    current = time.time()
    return (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current))
        + f".{int((current % 1) * 1000):03d}"
    )


def _init_boot_diag():
    """Open the dedicated startup log and arm the one-shot stack watchdog."""

    global _BOOT_DIAG
    try:
        log_file = open(
            os.path.join(tempfile.gettempdir(), "osia_server_boot.log"),
            "a",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        log_file.write(
            f"\n==== boot {_boot_ts()} pid={os.getpid()} "
            f"interp={sys.executable!r} ====\n"
        )
        log_file.flush()
        try:
            faulthandler.enable(file=log_file, all_threads=True)
            faulthandler.dump_traceback_later(
                45,
                repeat=False,
                file=log_file,
            )
        except Exception:
            pass
        _BOOT_DIAG = log_file
        return log_file
    except Exception:
        return None


def _boot_mark(message: str) -> None:
    """Write one flushed startup breadcrumb to the dedicated log."""

    log_file = _BOOT_DIAG
    if log_file is None:
        return
    try:
        log_file.write(f"{_boot_ts()} [boot] {message}\n")
        log_file.flush()
    except Exception:
        pass


def _bind_host() -> str:
    return "127.0.0.1" if os.environ.get("OSIA_SERVER_LOOPBACK_ONLY") == "1" else "0.0.0.0"


def _port_8765_available() -> bool:
    """Return whether uvicorn can own 8765, without leaving a socket behind."""

    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            # Match asyncio's POSIX bind behavior so TIME_WAIT is not mistaken
            # for a live listener. SO_REUSEADDR can hijack on Windows.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((_bind_host(), 8765))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _start_relay_connector() -> None:
    try:
        from osia.relay_connector import start_background_connector

        start_background_connector()
        _boot_mark("relay connector start requested")
    except Exception as exc:
        print(f"[OSIA Relay] connector start skipped: {exc}")


def _start_lan_gateway() -> None:
    try:
        from osia.transport_coordinator import (
            application_transport_coordination_enabled,
        )

        if application_transport_coordination_enabled():
            from osia.lan_gateway import start_background_gateway

            start_background_gateway()
            _boot_mark("LAN gateway start requested")
    except Exception as exc:
        print(f"[OSIA LAN] gateway start skipped: {exc}")


def _lan_coordination_enabled() -> bool:
    try:
        from osia.transport_coordinator import (
            application_transport_coordination_enabled,
        )

        return application_transport_coordination_enabled()
    except Exception:
        return False


def _run_uvicorn() -> None:
    _boot_mark("before uvicorn.run / app import")
    import uvicorn

    try:
        uvicorn.run(
            "osia.server.server:app",
            host=_bind_host(),
            port=8765,
            log_level="info",
        )
    finally:
        # Explicit, bounded LAN cleanup; a no-op when it never started.
        try:
            from osia.lan_gateway import stop_background_gateway

            stop_background_gateway()
        except Exception:
            pass


def _launch_after_port_guard(
    *,
    port_guard=_port_8765_available,
    prepare=lambda: None,
    start_relay=_start_relay_connector,
    start_lan=_start_lan_gateway,
    run_server=_run_uvicorn,
) -> bool:
    """Guard 8765 before creating any relay or LAN background component."""

    if not port_guard():
        _boot_mark("port 8765 already in use; exiting cleanly")
        print("port 8765 already in use; exiting cleanly")
        return False
    prepare()
    if _lan_coordination_enabled():
        # Provisioning must exist before the E2E relay standby announces it.
        start_lan()
        start_relay()
    else:
        # Preserve the established relay-only startup order when LAN is off.
        start_relay()
        start_lan()
    run_server()
    return True


def _configure_windows_streams() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # pythonw.exe can provide no stdio handles. Keep uvicorn's first log write
    # from killing the detached server; use only the OS temp directory.
    if sys.stdout is None or sys.stderr is None:
        try:
            log_file = open(
                os.path.join(tempfile.gettempdir(), "osia_server.log"),
                "a",
                encoding="utf-8",
                errors="replace",
            )
            if sys.stdout is None:
                sys.stdout = log_file
            if sys.stderr is None:
                sys.stderr = log_file
        except Exception:
            pass


def _prepare_runtime() -> None:
    _init_boot_diag()
    _boot_mark("interpreter up")
    _configure_windows_streams()

    # Bootstrap config before imports that read the persisted mobile token.
    _boot_mark("before ensure_config_files")
    try:
        from osia.config import ensure_config_files

        ensure_config_files()
    except Exception as exc:
        print(f"[CONFIG] ensure_config_files skipped: {exc}")
    _boot_mark("after ensure_config_files")
    try:
        from osia.image_inputs import (
            cleanup_media_store,
            cleanup_owned_temp_images,
        )

        cleanup_media_store()
        cleanup_owned_temp_images()
    except Exception:
        pass


def main() -> int:

    # The ownership guard is the first operation capable of reaching any
    # connector, gateway thread, or uvicorn application import.
    _launch_after_port_guard(prepare=_prepare_runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
