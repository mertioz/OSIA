"""Deterministic WOR-239 updater progress and banner integration coverage."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from osia import i18n, updater
from osia.gui import update_ui


PAYLOAD = (
    b"OSIA-WOR-239-real-progress-fixture-"
    * (2 * updater.DOWNLOAD_CHUNK_BYTES // 35 + 100)
)[: 2 * updater.DOWNLOAD_CHUNK_BYTES + 257]


def _metadata() -> updater.AppMetadata:
    return updater.AppMetadata(
        product="OSIA",
        version="1.0.1",
        channel="internal-preview",
        platform="windows",
        architecture="x86_64",
        build_timestamp="2026-08-06T12:00:00Z",
        commit_sha="a" * 40,
        updater_schema_version=1,
        build_override={"internalPreview": True},
    )


def _manifest(
    payload: bytes = PAYLOAD,
    *,
    sha256: str | None = None,
    version: str = "1.0.2",
) -> updater.UpdateManifest:
    raw = {
        "schemaVersion": 1,
        "product": "OSIA",
        "platform": "windows",
        "architecture": "x86_64",
        "channel": "internal-preview",
        "version": version,
        "publishedAt": "2026-08-06T12:00:00Z",
        "installer": {
            "url": f"https://download.osia.space/windows/{version}/OSIA-Setup.exe",
            "sizeBytes": len(payload),
            "sha256": sha256 or hashlib.sha256(payload).hexdigest(),
            "signed": False,
            "signaturePublisher": None,
        },
        "mandatory": False,
        "minimumSupportedVersion": "1.0.0",
        "minimumUpdaterVersion": "1.0.1",
        "releaseNotes": ["WOR-239 fixture"],
        "releaseNotesUrl": None,
        "rollout": {"state": "enabled"},
    }
    return updater.parse_manifest(json.dumps(raw), _metadata())


@dataclass
class _Route:
    payload: bytes = PAYLOAD
    include_length: bool = True
    advertised_length: int | None = None
    interrupt_first_after: int | None = None
    delay: float = 0.001
    requests: int = 0


class _LocalResponse:
    def __init__(self, connection, response):
        self.connection = connection
        self.response = response
        self.status = response.status
        self.headers = response.headers
        self.closed = False

    def read(self, amount=-1):
        return self.response.read(amount)

    def close(self):
        self.response.close()
        self.connection.close()
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@contextmanager
def _http_fixture(route: _Route):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802 - stdlib handler contract
            route.requests += 1
            request_number = route.requests
            self.send_response(200)
            if route.include_length:
                declared = (
                    len(route.payload)
                    if route.advertised_length is None
                    else route.advertised_length
                )
                self.send_header("Content-Length", str(declared))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            sent = 0
            for offset in range(0, len(route.payload), 64 * 1024):
                chunk = route.payload[offset:offset + 64 * 1024]
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                sent += len(chunk)
                if (
                    request_number == 1
                    and route.interrupt_first_after is not None
                    and sent >= route.interrupt_first_after
                ):
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if route.delay:
                    time.sleep(route.delay)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    responses: list[_LocalResponse] = []

    def opener(_url, headers, connect_timeout, read_timeout):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port,
            timeout=max(connect_timeout, read_timeout),
        )
        connection.request("GET", "/installer", headers=headers)
        wrapped = _LocalResponse(connection, connection.getresponse())
        responses.append(wrapped)
        return wrapped

    try:
        yield opener, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _wait_until(app, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


def _controller(monkeypatch, tmp_path, manifest, opener, process_factory=None):
    monkeypatch.setattr(update_ui, "load_app_metadata", _metadata)
    monkeypatch.setattr(update_ui, "updater_enabled", lambda _value: True)
    controller = update_ui.UpdateController(
        store=updater.UpdateStateStore(tmp_path / "state.json"),
        updates_dir=tmp_path / "updates",
        open_response=opener,
        process_factory=process_factory,
    )
    controller._timer.stop()
    controller.manifest = manifest
    controller._publish(
        status="available",
        availableVersion=manifest.version,
        availableChannel=manifest.channel,
        releaseNotes=manifest.release_notes,
        bannerVisible=True,
        error=None,
    )
    return controller


def test_new_manifest_downloads_its_own_installer_after_cancel(app, monkeypatch, tmp_path):
    previous = _manifest(b"old")
    route = _Route(payload=b"new")
    current = _manifest(route.payload, version="1.0.3")
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Cancel,
    )
    with _http_fixture(route) as (opener, _):
        controller = _controller(monkeypatch, tmp_path, previous, opener)
        cache = updater.installer_path(previous.version, tmp_path / "updates")
        cache.parent.mkdir()
        cache.write_bytes(b"old")
        controller.installer = cache
        assert controller.scheduler.begin()
        assert controller.install() is False
        controller._on_check_finished(
            updater.ManifestFetchResult("available", current), True, None, controller.preview_opt_in,
        )
        controller._timer.stop()
        assert controller.installer is None
        assert controller.install()
        assert _wait_until(app, lambda: controller.operation.snapshot.phase == "idle")
        assert route.requests == 1
        assert controller.installer.read_bytes() == route.payload
        assert controller.installer.name == "1.0.3-OSIA-Setup.exe"


def test_snapshots_are_immutable_and_french_progress_is_concise(app):
    operation = updater.UpdaterStateMachine()
    preparing = operation.begin()
    with pytest.raises(FrozenInstanceError):
        preparing.phase = "downloading"
    operation.downloading(1_160_000_000)
    snapshot = operation.advance(498_000_000, 1_160_000_000)
    state = {
        "operationSnapshot": snapshot,
        "availableVersion": "1.0.2",
        "bannerVisible": True,
        "majorUpdate": False,
    }
    original_language = i18n.get_language()
    try:
        i18n.set_language("fr", persist=False)
        banner = update_ui.UpdateBanner()
        banner.apply_state(state)
        assert banner.label.text() == (
            "Téléchargement 42 % — 498 Mo / 1,16 Go"
        )
        banner.close()
    finally:
        i18n.set_language(original_language, persist=False)


def test_state_machine_begin_is_atomic_under_concurrent_clicks():
    operation = updater.UpdaterStateMachine()
    barrier = threading.Barrier(12)
    results = []

    def click():
        barrier.wait()
        results.append(operation.begin())

    threads = [threading.Thread(target=click) for _index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert sum(result is not None for result in results) == 1
    assert operation.snapshot.phase == "preparing"
    assert operation.snapshot.attempt == 1


def test_slow_known_length_reports_written_bytes_and_closes_before_100(tmp_path):
    route = _Route()
    observed = []
    response_box = []

    def listener(snapshot):
        closed = bool(response_box and response_box[-1].closed)
        observed.append((snapshot, closed))

    operation = updater.UpdaterStateMachine(listener)
    assert operation.begin().phase == "preparing"
    with _http_fixture(route) as (opener, responses):
        response_box = responses
        path = updater.download_installer(
            _manifest(), updates_dir=tmp_path,
            open_response=opener, operation=operation,
        )

    downloads = [item for item in observed if item[0].phase == "downloading"]
    assert len(downloads) >= 2
    assert [item[0].bytes_received for item in downloads] == sorted(
        item[0].bytes_received for item in downloads
    )
    assert all(item[0].total_bytes == len(PAYLOAD) for item in downloads)
    assert all(item[0].percentage is not None for item in downloads)
    assert all(item[0].percentage < 100 for item in downloads)
    verifying = [item for item in observed if item[0].phase == "verifying"][-1]
    assert verifying[0].bytes_received == len(PAYLOAD)
    assert verifying[0].percentage == 100
    assert verifying[1] is True
    assert path.read_bytes() == PAYLOAD


def test_download_without_content_length_is_honestly_indeterminate(tmp_path):
    route = _Route(include_length=False)
    snapshots = []
    operation = updater.UpdaterStateMachine(snapshots.append)
    operation.begin()
    with _http_fixture(route) as (opener, _responses):
        path = updater.download_installer(
            _manifest(), updates_dir=tmp_path,
            open_response=opener, operation=operation,
        )

    downloads = [state for state in snapshots if state.phase == "downloading"]
    assert downloads
    assert all(state.total_bytes is None for state in downloads)
    assert all(state.percentage is None for state in downloads)
    assert all(state.indeterminate for state in downloads)
    assert path.read_bytes() == PAYLOAD


def test_network_interruption_cleans_partial_and_retry_is_one_clean_attempt(tmp_path):
    route = _Route(interrupt_first_after=updater.DOWNLOAD_CHUNK_BYTES + 1)
    operation = updater.UpdaterStateMachine()
    operation.begin()
    with _http_fixture(route) as (opener, _responses):
        with pytest.raises(updater.DownloadError, match="download_interrupted"):
            updater.download_installer(
                _manifest(), updates_dir=tmp_path,
                open_response=opener, operation=operation,
            )
        assert operation.snapshot.phase == "failed"
        assert not updater.partial_path("1.0.2", tmp_path).exists()
        assert operation.begin().attempt == 2
        path = updater.download_installer(
            _manifest(), updates_dir=tmp_path,
            open_response=opener, operation=operation,
        )

    assert route.requests == 2
    assert path.read_bytes() == PAYLOAD
    assert not updater.partial_path("1.0.2", tmp_path).exists()


def test_corrupt_cached_installer_is_replaced_by_one_verified_download(tmp_path):
    final = updater.installer_path("1.0.2", tmp_path)
    final.write_bytes(b"corrupt")
    route = _Route()
    operation = updater.UpdaterStateMachine()
    operation.begin()
    with _http_fixture(route) as (opener, _responses):
        path = updater.download_installer(
            _manifest(), updates_dir=tmp_path,
            open_response=opener, operation=operation,
        )
    assert route.requests == 1
    assert path.read_bytes() == PAYLOAD
    assert operation.snapshot.phase == "verifying"


def test_insufficient_disk_space_fails_before_network_and_cleans(monkeypatch, tmp_path):
    route = _Route()
    operation = updater.UpdaterStateMachine()
    operation.begin()
    monkeypatch.setattr(
        updater.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0),
    )
    with _http_fixture(route) as (opener, _responses):
        with pytest.raises(updater.DownloadError, match="insufficient_disk_space"):
            updater.download_installer(
                _manifest(), updates_dir=tmp_path,
                open_response=opener, operation=operation,
            )
    assert route.requests == 0
    assert operation.snapshot.phase == "failed"
    assert operation.snapshot.error == "insufficient_disk_space"
    assert not updater.partial_path("1.0.2", tmp_path).exists()


@pytest.mark.parametrize("failure", ["size", "sha256"])
def test_verification_failure_never_launches(
    monkeypatch, tmp_path, app, failure,
):
    route = _Route(
        advertised_length=(len(PAYLOAD) + 1) if failure == "size" else None,
    )
    manifest = _manifest(sha256="0" * 64 if failure == "sha256" else None)
    launched = []
    with _http_fixture(route) as (opener, _responses):
        controller = _controller(
            monkeypatch, tmp_path, manifest, opener,
            process_factory=lambda *args, **kwargs: launched.append((args, kwargs)),
        )
        assert controller.install()
        assert _wait_until(app, lambda: controller.state["status"] == "failed")

    assert launched == []
    assert controller.installer is None
    assert not updater.partial_path(
        manifest.version, tmp_path / "updates",
    ).exists()
    assert not updater.installer_path(
        manifest.version, tmp_path / "updates",
    ).exists()
    controller._timer.stop()


def test_double_click_widget_recreation_and_valid_launch_transition(
    monkeypatch, tmp_path, app,
):
    route = _Route(delay=0.003)
    launches = []

    def process_factory(command, **kwargs):
        launches.append((command, kwargs))
        return object()

    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    with _http_fixture(route) as (opener, _responses):
        controller = _controller(
            monkeypatch, tmp_path, _manifest(), opener,
            process_factory=process_factory,
        )
        banner = update_ui.UpdateBanner()
        controller.state_changed.connect(banner.apply_state)
        banner.apply_state(controller.state)
        statuses = []
        controller.state_changed.connect(
            lambda state: statuses.append(state.get("status"))
        )

        started = time.monotonic()
        assert controller.install(banner) is True
        assert time.monotonic() - started < 0.1
        assert controller.state["status"] == "preparing"
        assert banner.label.text() == i18n.t("updates.phase.preparing")
        assert controller.install(banner) is False
        assert _wait_until(app, lambda: controller.state["status"] == "downloading")

        recreated = update_ui.UpdateBanner()
        recreated.apply_state(controller.state)
        assert recreated.label.text() == banner.label.text()
        assert recreated.progress.isVisible()
        banner.clearFocus()
        banner.resize(620, banner.height())
        banner.apply_state(controller.state)
        assert controller.state["status"] == "downloading"

        assert _wait_until(app, lambda: controller.state["status"] == "launching")

    ordered = [
        next(index for index, status in enumerate(statuses) if status == phase)
        for phase in ("preparing", "downloading", "verifying", "launching")
    ]
    assert ordered == sorted(ordered)
    assert route.requests == 1
    assert len(launches) == 1
    command, kwargs = launches[0]
    assert "/OSIAUPDATE=1" in command
    assert kwargs["shell"] is False
    assert not updater.partial_path("1.0.2", tmp_path / "updates").exists()
    controller._timer.stop()
    banner.close()
    recreated.close()


def test_unknown_length_banner_is_localized_and_retry_deduplicates(
    monkeypatch, tmp_path, app,
):
    route = _Route(include_length=False, delay=0.003)
    original_language = i18n.get_language()
    try:
        i18n.set_language("fr", persist=False)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel,
        )
        with _http_fixture(route) as (opener, _responses):
            controller = _controller(monkeypatch, tmp_path, _manifest(), opener)
            banner = update_ui.UpdateBanner()
            controller.state_changed.connect(banner.apply_state)
            banner.apply_state(controller.state)
            assert controller.install(banner)
            assert not controller.install(banner)
            assert _wait_until(
                app,
                lambda: (
                    controller.state["status"] == "downloading"
                    and controller.state["progressIndeterminate"]
                ),
            )
            assert banner.label.text() == "Téléchargement…"
            assert banner.progress.minimum() == 0
            assert banner.progress.maximum() == 0
            assert _wait_until(app, lambda: controller.state["status"] == "available")
        assert route.requests == 1
        controller._timer.stop()
        banner.close()
    finally:
        i18n.set_language(original_language, persist=False)
