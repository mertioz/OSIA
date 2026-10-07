"""Single entry point for the packaged Windows desktop and background modes."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path



from osia.runtime import (
    DOCUMENT_WORKER_MODE,
    INSTALL_INTEGRATION_MODE,
    PACKAGE_SMOKE_MODE,
    PDF_RENDER_WORKER_MODE,
    MEDIA_WORKER_MODE,
    RELAUNCH_MODE,
    REMOVE_INTEGRATION_MODE,
    SERVER_MODE,
    SERVICE_MODE,
    WAKE_RECOVERY_MODE,
    bundled_model_path,
    is_frozen,
    resource_path,
)

BROWSER_SMOKE_MODE = "--osia-browser-smoke"
# The old flag remains an alias for already-installed uninstaller records.
REMOVE_LEGACY_BROWSER_REGISTRATION_MODE = "--osia-remove-legacy-browser-registration"
REMOVE_PERSONAL_BROWSER_REGISTRATION_ALIAS = "--osia-remove-personal-browser-registration"
_PACKAGE_SMOKE_REPORT_ENV = "OSIA_PACKAGE_SMOKE_REPORT"


def _write_package_smoke_report(
    status: str, step: str, error_type: str = "",
) -> None:
    """Write one path/content-free diagnostic for the trusted build script."""

    raw = os.environ.get(_PACKAGE_SMOKE_REPORT_ENV, "")
    if not raw:
        return
    try:
        target = Path(raw).resolve(strict=False)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        name = target.name
        prefix = "osia-package-smoke-result-"
        suffix = ".txt"
        token = name[len(prefix):-len(suffix)]
        if (
            target.parent != temp_root
            or not name.startswith(prefix)
            or not name.endswith(suffix)
            or len(token) != 16
            or any(char not in "0123456789abcdef" for char in token)
        ):
            return
        target.write_text(
            f"status={status}\nstep={step}\nerror_type={error_type}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


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
    step = "resources"
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
        _write_package_smoke_report("failed", step, "MissingResource")
        print("ERROR: missing packaged resources: " + ", ".join(missing))
        return 1
    try:
        step = "imports"
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
        from osia.document_extractors.smoke import run_packaged_document_smoke
        from osia.media_reader import (
            run_packaged_media_smoke,
            verify_packaged_dependencies as verify_media_dependencies,
        )
        from osia.pdf_renderer import (
            verify_packaged_dependencies as verify_pdf_renderer_dependencies,
        )

        step = "metadata"
        metadata = load_app_metadata()
        if metadata.product != "OSIA" or metadata.platform != "windows":
            raise RuntimeError("embedded app metadata is invalid")

        step = "playwright"
        node, cli = compute_driver_executable()
        if not os.path.isfile(node) or not os.path.isfile(cli):
            raise RuntimeError("Playwright driver is incomplete")

        step = "qt"
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        qt_app = QApplication.instance() or QApplication([])
        if not qt_app.platformName():
            raise RuntimeError("Qt platform plugin did not initialize")
        if QPixmap(LOGO_PATH).isNull():
            raise RuntimeError("Qt could not load the packaged logo")

        step = "release_notes"
        if release_for_version(metadata.version) is None:
            raise RuntimeError("embedded release notes are unavailable")
        step = "pdf_ocr_dependencies"
        if not verify_packaged_dependencies():
            raise RuntimeError("local PDF/OCR dependencies are incomplete")
        step = "pdf_renderer_dependencies"
        if not verify_pdf_renderer_dependencies():
            raise RuntimeError("local PDF renderer dependencies are incomplete")
        step = "media_dependencies"
        if not verify_media_dependencies():
            raise RuntimeError("local media dependencies are incomplete")
        step = "media_workers"
        with tempfile.TemporaryDirectory(prefix="osia-media-smoke-") as temporary:
            if not run_packaged_media_smoke(Path(temporary)):
                raise RuntimeError("offline image/audio/video smoke failed")
        step = "structured_documents"
        with tempfile.TemporaryDirectory(prefix="osia-document-smoke-") as temporary:
            if not run_packaged_document_smoke(Path(temporary)):
                raise RuntimeError("offline structured-document smoke failed")
        step = "onboarding_state"
        with tempfile.TemporaryDirectory(prefix="osia-package-smoke-") as temporary:
            state_path = Path(temporary) / "onboarding.json"
            if not create_fresh_install_marker(state_path):
                raise RuntimeError("fresh-install onboarding marker was not created")
            if not claim_automatic_onboarding(state_path):
                raise RuntimeError("fresh-install onboarding marker was not claimed")
            mark_onboarding_handled("completed", state_path)
            if onboarding_state(state_path).get("outcome") != "completed":
                raise RuntimeError("onboarding completion did not persist")
        step = "onboarding_ui"
        onboarding = OnboardingDialog(automatic=False)
        if (
            onboarding.ESSENTIAL_COUNT < 7
            or onboarding.MASTERY_COUNT < 16
            or onboarding.ADVANCED_COUNT < 38
        ):
            raise RuntimeError("packaged OSIA guide is incomplete")
        onboarding.deleteLater()
        qt_app.processEvents()

        step = "uiautomation"
        if uiautomation.GetRootControl() is None:
            raise RuntimeError("Windows UI Automation did not initialize")
        if _DllClient.instance().dll is None:
            raise RuntimeError("uiautomation native bitmap client did not initialize")

        step = "codex_runtime"
        codex_path = resource_path("runtimes", "codex", "bin", "codex.exe")
        codex_version = AppServerClient(
            runtime_executable=codex_path,
        ).get_runtime_version()
        if not codex_version or "0.144.1" not in codex_version:
            raise RuntimeError("managed Codex runtime did not initialize")

        step = "whisper_model"
        whisper_path = bundled_model_path("faster-whisper-small")
        WhisperModel(
            str(whisper_path), device="cpu", compute_type="int8", local_files_only=True,
        )

        step = "tts"
        from osia.voice.tts import _get_kokoro_pipeline

        pipeline = _get_kokoro_pipeline("a")
        segment = next(iter(pipeline("OSIA", voice="af_sky", speed=1.0)), None)
        if segment is None or segment[2] is None:
            raise RuntimeError("Kokoro produced no audio")

        step = "mp3"
        output = io.BytesIO()
        container = av.open(output, mode="w", format="mp3")
        stream = container.add_stream("libmp3lame", rate=24000)
        stream.bit_rate = 128000
        container.close()
    except Exception as exc:
        _write_package_smoke_report("failed", step, type(exc).__name__)
        print(f"ERROR: packaged runtime smoke failed: {type(exc).__name__}: {exc}")
        return 1
    _write_package_smoke_report("ok", "complete")
    print(f"OSIA package smoke OK (frozen={is_frozen()})")
    return 0


def _browser_smoke() -> int:
    """Exercise the frozen browser subsystem against loopback only."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from osia.tools import browser
    from osia.tools.browser_artifacts import (
        BrowserArtifactStore,
        set_browser_artifact_store_for_testing,
    )

    download_bytes = b"OSIA frozen browser smoke download\n"

    class Handler(BaseHTTPRequestHandler):
        def _download_headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(download_bytes)))
            self.send_header(
                "Content-Disposition", "attachment; filename=browser-smoke.txt",
            )
            self.end_headers()

        def do_HEAD(self):  # noqa: N802 - stdlib handler API
            if self.path == "/download":
                self._download_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):  # noqa: N802 - stdlib handler API
            if self.path == "/download":
                self._download_headers()
                self.wfile.write(download_bytes)
                return
            body = (
                b"<!doctype html><main><h1>OSIA browser smoke</h1>"
                b'<a href="/download" download>Safe download</a></main>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    binary = browser._detect_chromium_browser()
    if not binary:
        print("ERROR: no compatible installed system browser was discovered")
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = (
        browser.CDP_PORT,
        browser.CDP_URL,
        browser.OSIA_PROFILE_DIR,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="osia-browser-smoke-") as temporary:
            smoke_profile = str(Path(temporary) / "profile")
            smoke_store = BrowserArtifactStore(Path(temporary) / "artifacts")
            previous_store = set_browser_artifact_store_for_testing(smoke_store)
            try:
                browser.OSIA_PROFILE_DIR = smoke_profile
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    browser.CDP_PORT = int(probe.getsockname()[1])
                browser.CDP_URL = f"http://127.0.0.1:{browser.CDP_PORT}"
                opened = browser._owner.call(
                    "open_cdp_browser", binary,
                    f"http://127.0.0.1:{server.server_port}/",
                    timeout=45.0,
                )
                if not isinstance(opened, dict) or opened.get("ok") is not True:
                    raise RuntimeError("browser did not produce a verified navigation")
                state = (opened.get("data") or {}).get("state")
                inspected = browser.inspect_browser_page(state)
                inspected_data = inspected.get("data") or {}
                headings = inspected_data.get("headings", [])
                if not inspected.get("ok") or not any(
                    item.get("text") == "OSIA browser smoke" for item in headings
                ):
                    raise RuntimeError("structured loopback inspection failed")
                links = inspected_data.get("links") or []
                download_link = next(
                    (
                        item for item in links
                        if str(item.get("safe_destination") or "").endswith(
                            "/download"
                        )
                    ),
                    None,
                )
                if not isinstance(download_link, dict):
                    raise RuntimeError("safe download reference missing")
                downloaded = browser.download_browser_file(
                    state, str(download_link.get("element_ref") or ""),
                )
                artifact = (downloaded.get("data") or {}).get("artifact") or {}
                if downloaded.get("ok") is not True or not artifact.get("artifact_id"):
                    raise RuntimeError("private browser download failed")
                stored = smoke_store.inspect(str(artifact["artifact_id"]))
                if not (
                    stored.get("size") == len(download_bytes)
                    and stored.get("sha256")
                    == hashlib.sha256(download_bytes).hexdigest()
                    and stored.get("private") is True
                    and stored.get("executable") is False
                    and stored.get("extractable") is False
                ):
                    raise RuntimeError("private browser artifact verification failed")
            finally:
                set_browser_artifact_store_for_testing(previous_store)
                browser.close_browser()
                leftovers = browser._processes_using_profile(smoke_profile)
                if leftovers:
                    browser._terminate_processes(leftovers)
        print("OSIA frozen browser smoke OK")
        return 0
    except Exception as exc:
        print(f"ERROR: frozen browser smoke failed: {type(exc).__name__}")
        return 1
    finally:
        browser.close_browser()
        browser.CDP_PORT, browser.CDP_URL, browser.OSIA_PROFILE_DIR = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _acquire_gui_mutex():
    """Keep one desktop instance across source and packaged entry points."""
    from osia.relaunch import acquire_desktop_instance_mutex

    return acquire_desktop_instance_mutex()


def main(args: list[str] | None = None) -> int:
    argv = _normalized_args(args)
    mode = argv[0] if argv else ""

    if mode == DOCUMENT_WORKER_MODE:
        if len(argv) != 2:
            return 2
        from osia.document_reader import worker_main

        return worker_main(argv[1])
    if mode == MEDIA_WORKER_MODE:
        if len(argv) != 2:
            return 2
        from osia.media_reader import worker_main

        return worker_main(argv[1])
    if mode == PDF_RENDER_WORKER_MODE:
        if len(argv) != 2:
            return 2
        from osia.pdf_renderer import worker_main

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
    if mode == BROWSER_SMOKE_MODE:
        return _browser_smoke()
    if mode in {
        REMOVE_LEGACY_BROWSER_REGISTRATION_MODE,
        REMOVE_PERSONAL_BROWSER_REGISTRATION_ALIAS,
    }:
        from osia.legacy_browser_cleanup import remove_legacy_browser_registration

        result = remove_legacy_browser_registration()
        return 0 if result.get("removed") or result.get("state") in {
            "not_registered", "not_applicable",
        } else 1
    if is_frozen() and sys.platform == "win32":
        try:
            from osia.legacy_browser_cleanup import (
                remove_legacy_browser_registration,
            )

            remove_legacy_browser_registration()
        except Exception:
            # An unavailable or already-removed legacy registration must not
            # prevent the supported isolated browser from starting.
            pass
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
