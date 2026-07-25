"""Single entry point for the packaged Windows desktop and background modes."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from osia.runtime import (
    DOCUMENT_WORKER_MODE,
    INSTALL_INTEGRATION_MODE,
    PACKAGE_SMOKE_MODE,
    RELAUNCH_MODE,
    REMOVE_INTEGRATION_MODE,
    SERVER_MODE,
    SERVICE_MODE,
    WAKE_RECOVERY_MODE,
    bundled_model_path,
    is_frozen,
    resource_path,
)


def _normalized_args(args: list[str] | None = None) -> list[str]:
    """Drop the duplicated executable inserted by a frozen ``os.execv`` restart."""
    values = list(sys.argv[1:] if args is None else args)
    if values and os.path.normcase(os.path.abspath(values[0])) == os.path.normcase(
        os.path.abspath(sys.executable)
    ):
        values.pop(0)
    return values


def _package_smoke() -> int:
    """Offline dependency/model probe used by build and install validation."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from osia.utils.assets import ICON_PATH, LOGO_PATH, asset_path
    from osia.guide_content import LESSONS

    required = [
        ("app metadata", resource_path("app_metadata.json")),
        ("icon", ICON_PATH),
        ("logo", LOGO_PATH),
        ("whisper", bundled_model_path("faster-whisper-small")),
        ("kokoro", bundled_model_path("kokoro")),
        ("codex", resource_path("runtimes", "codex", "bin", "codex.exe")),
    ]
    required.extend(
        (f"guide asset {lesson.asset}", asset_path(lesson.asset))
        for lesson in LESSONS
        if lesson.asset
    )
    missing = [label for label, path in required if path is None or not os.path.exists(path)]
    if missing:
        print("ERROR: missing packaged resources: " + ", ".join(missing))
        return 1
    try:
        import io

        import aiortc  # noqa: F401
        import av
        import mcp  # noqa: F401
        import openwakeword  # noqa: F401
        import uiautomation
        import win32com.client  # noqa: F401
        from uiautomation.uiautomation import _DllClient
        from PyQt6.QtGui import QPixmap
        from PyQt6.QtWidgets import QApplication
        from faster_whisper import WhisperModel
        from playwright._impl._driver import compute_driver_executable
        from osia.chatgpt_account.app_server_client import AppServerClient
        from osia.gui.onboarding import OnboardingDialog
        from osia.onboarding import (
            claim_automatic_onboarding,
            create_fresh_install_marker,
            mark_onboarding_handled,
            onboarding_state,
        )
        from osia.release_notes import release_for_version
        from osia.updater import load_app_metadata
        from osia.document_reader import verify_packaged_dependencies

        metadata = load_app_metadata()
        if metadata.product != "OSIA" or metadata.platform != "windows":
            raise RuntimeError("embedded app metadata is invalid")

        node, cli = compute_driver_executable()
        if not os.path.isfile(node) or not os.path.isfile(cli):
            raise RuntimeError("Playwright driver is incomplete")

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        qt_app = QApplication.instance() or QApplication([])
        if not qt_app.platformName():
            raise RuntimeError("Qt platform plugin did not initialize")
        if QPixmap(LOGO_PATH).isNull():
            raise RuntimeError("Qt could not load the packaged logo")

        if release_for_version(metadata.version) is None:
            raise RuntimeError("embedded release notes are unavailable")
        if not verify_packaged_dependencies():
            raise RuntimeError("local PDF/OCR dependencies are incomplete")
        with tempfile.TemporaryDirectory(prefix="osia-package-smoke-") as temporary:
            state_path = Path(temporary) / "onboarding.json"
            if not create_fresh_install_marker(state_path):
                raise RuntimeError("fresh-install onboarding marker was not created")
            if not claim_automatic_onboarding(state_path):
                raise RuntimeError("fresh-install onboarding marker was not claimed")
            mark_onboarding_handled("completed", state_path)
            if onboarding_state(state_path).get("outcome") != "completed":
                raise RuntimeError("onboarding completion did not persist")
        onboarding = OnboardingDialog(automatic=False)
        if (
            onboarding.ESSENTIAL_COUNT < 7
            or onboarding.MASTERY_COUNT < 16
            or onboarding.ADVANCED_COUNT < 38
        ):
            raise RuntimeError("packaged OSIA guide is incomplete")
        onboarding.deleteLater()
        qt_app.processEvents()

        if uiautomation.GetRootControl() is None:
            raise RuntimeError("Windows UI Automation did not initialize")
        if _DllClient.instance().dll is None:
            raise RuntimeError("uiautomation native bitmap client did not initialize")

        codex_path = resource_path("runtimes", "codex", "bin", "codex.exe")
        codex_version = AppServerClient(
            runtime_executable=codex_path,
        ).get_runtime_version()
        if not codex_version or "0.144.1" not in codex_version:
            raise RuntimeError("managed Codex runtime did not initialize")

        whisper_path = bundled_model_path("faster-whisper-small")
        WhisperModel(
            str(whisper_path), device="cpu", compute_type="int8", local_files_only=True,
        )

        from osia.voice.tts import _get_kokoro_pipeline

        pipeline = _get_kokoro_pipeline("a")
        segment = next(iter(pipeline("OSIA", voice="af_sky", speed=1.0)), None)
        if segment is None or segment[2] is None:
            raise RuntimeError("Kokoro produced no audio")

        output = io.BytesIO()
        container = av.open(output, mode="w", format="mp3")
        stream = container.add_stream("libmp3lame", rate=24000)
        stream.bit_rate = 128000
        container.close()
    except Exception as exc:
        print(f"ERROR: packaged runtime smoke failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"OSIA package smoke OK (frozen={is_frozen()})")
    return 0


def _acquire_gui_mutex():
    """Keep one desktop instance per Windows login session."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "Local\\OSIA.Desktop")
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    return handle


def main(args: list[str] | None = None) -> int:
    argv = _normalized_args(args)
    mode = argv[0] if argv else ""

    if mode == DOCUMENT_WORKER_MODE:
        if len(argv) != 2:
            return 2
        from osia.document_reader import worker_main

        return worker_main(argv[1])
    if mode == SERVICE_MODE:
        if len(argv) != 2 or argv[1] != "server":
            return 2
        from windows.run_service import main as service_main

        return service_main(["server"])
    if mode == SERVER_MODE:
        from osia.server.run_server import main as server_main

        return int(server_main() or 0)
    if mode == WAKE_RECOVERY_MODE:
        from windows.wake_recovery import main as wake_main

        return wake_main()
    if mode == INSTALL_INTEGRATION_MODE:
        from osia.utils.systemd_manager import install_windows_services

        return 0 if install_windows_services() else 1
    if mode == REMOVE_INTEGRATION_MODE:
        from osia.utils.systemd_manager import remove_windows_services

        return 0 if remove_windows_services() else 1
    if mode == PACKAGE_SMOKE_MODE:
        return _package_smoke()
    if mode == RELAUNCH_MODE:
        if len(argv) != 2:
            return 2
        try:
            parent_pid = int(argv[1])
        except (TypeError, ValueError):
            return 2
        if parent_pid <= 0:
            return 2
        from osia.relaunch import run_coordinator

        return run_coordinator(parent_pid)

    mutex = _acquire_gui_mutex()
    if mutex is False:
        return 0
    from osia.main import main as desktop_main

    original = sys.argv
    try:
        sys.argv = [original[0], *argv]
        return int(desktop_main() or 0)
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
