"""Security and state-machine tests for the Windows application updater."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from osia import updater


def metadata(version="1.0.1", channel="internal-preview"):
    payload = {
        "product": "OSIA",
        "version": version,
        "updateChannel": channel,
        "platform": "windows",
        "architecture": "x86_64",
        "buildTimestamp": "2026-07-20T10:00:00Z",
        "commitSha": "a" * 40,
        "updaterSchemaVersion": 1,
        "buildOverride": None,
    }
    return updater.parse_app_metadata(json.dumps(payload))


def manifest_dict(data=b"installer", *, version="1.0.2", channel="internal-preview"):
    return {
        "schemaVersion": 1,
        "product": "OSIA",
        "platform": "windows",
        "architecture": "x86_64",
        "channel": channel,
        "version": version,
        "publishedAt": "2026-07-20T10:01:00Z",
        "installer": {
            "url": f"https://download.osia.space/windows/{version}/OSIA-Setup.exe",
            "sizeBytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "signed": False,
            "signaturePublisher": None,
        },
        "mandatory": False,
        "minimumSupportedVersion": "1.0.1",
        "minimumUpdaterVersion": "1.0.1",
        "releaseNotes": ["Updater validation."],
        "releaseNotesUrl": None,
        "rollout": {"state": "enabled"},
    }


def parse_manifest(data=b"installer", **kwargs):
    raw = manifest_dict(data, **kwargs)
    return updater.parse_manifest(json.dumps(raw), metadata(channel=raw["channel"]))


class FakeResponse:
    def __init__(self, status, payload=b"", headers=None, *, fail_after=None):
        self.status = status
        self.headers = headers or {}
        self._stream = io.BytesIO(payload)
        self._read = 0
        self._fail_after = fail_after

    def read(self, amount=-1):
        if self._fail_after is not None and self._read >= self._fail_after:
            raise OSError("fixture interruption")
        chunk = self._stream.read(amount)
        self._read += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def opener(response, captured=None):
    def call(url, headers, connect_timeout, read_timeout):
        if captured is not None:
            captured.update(
                url=url, headers=headers, connect=connect_timeout, read=read_timeout
            )
        return response
    return call


def test_valid_manifest_and_version_comparison():
    parsed = parse_manifest()
    assert parsed.version == "1.0.2"
    assert updater.compare_versions("1.0.2", "1.0.1") == 1
    assert updater.compare_versions("1.0.1", "1.0.1") == 0


def test_app_metadata_is_strict_and_records_only_internal_override():
    raw = {
        "product": "OSIA", "version": "1.0.1", "updateChannel": "internal-preview",
        "platform": "windows", "architecture": "x86_64",
        "buildTimestamp": "2026-07-20T10:00:00Z", "commitSha": None,
        "updaterSchemaVersion": 1,
        "buildOverride": {
            "sourceVersion": "1.0.3", "version": "1.0.1",
            "channel": "internal-preview",
        },
    }
    assert updater.parse_app_metadata(json.dumps(raw)).build_override is not None
    raw["unexpected"] = True
    with pytest.raises(updater.MetadataError, match="unknown_metadata_field"):
        updater.parse_app_metadata(json.dumps(raw))


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("extra",), True, "unknown_manifest_field"),
        (("schemaVersion",), 2, "unsupported_schema"),
        (("schemaVersion",), "1", "invalid_schema"),
        (("channel",), "stable", "channel_mismatch"),
        (("platform",), "linux", "platform_mismatch"),
        (("architecture",), "arm64", "architecture_mismatch"),
        (("version",), "1.0.1", "equal_version"),
        (("version",), "1.0.0", "downgrade"),
        (("version",), "01.0.2", "invalid_version"),
        (("installer", "url"), "https://evil.example/windows/1.0.2/OSIA-Setup.exe", "invalid_installer_url"),
        (("installer", "url"), "https://download.osia.space/windows/1.0.2/other.exe", "invalid_installer_url"),
        (("installer", "url"), "https://download.osia.space/windows/1.0.2/OSIA-Setup.exe?q=1", "invalid_installer_url"),
        (("installer", "sha256"), "ABC", "invalid_sha256"),
        (("installer", "sizeBytes"), 0, "invalid_size"),
        (("installer", "sizeBytes"), updater.INSTALLER_MAX_BYTES + 1, "invalid_size"),
        (("rollout", "state"), "ramping", "unsupported_rollout_state"),
        (("releaseNotes",), ["<b>html</b>"], "invalid_release_notes"),
    ],
)
def test_manifest_rejections(path, value, code):
    raw = manifest_dict()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(updater.ManifestError, match=code):
        updater.parse_manifest(json.dumps(raw), metadata())


def test_duplicate_keys_and_non_finite_json_are_rejected():
    with pytest.raises(updater.ManifestError, match="duplicate_key"):
        updater.parse_manifest('{"schemaVersion":1,"schemaVersion":1}', metadata())
    raw = json.dumps(manifest_dict()).replace('"schemaVersion": 1', '"schemaVersion": NaN')
    with pytest.raises(updater.ManifestError, match="non_finite_number"):
        updater.parse_manifest(raw, metadata())


def test_stable_unsigned_is_rejected_and_stable_install_fails_closed():
    raw = manifest_dict(channel="stable")
    with pytest.raises(updater.ManifestError, match="stable_unsigned"):
        updater.parse_manifest(json.dumps(raw), metadata(channel="stable"))
    internal = parse_manifest()
    signed = replace(internal, channel="stable", installer=replace(
        internal.installer, signed=True, signature_publisher="OSIA"
    ))
    with pytest.raises(updater.DownloadError, match="stable_authenticode_unavailable"):
        updater.prepare_installer_plan(signed, Path("missing.exe"))


def test_fetch_uses_validators_and_304_cache(tmp_path):
    store = updater.UpdateStateStore(tmp_path / "state.json")
    raw = manifest_dict()
    store.update(etag='"old"', lastModified="Sun, 20 Jul 2026 10:00:00 GMT")
    captured = {}
    response = FakeResponse(200, json.dumps(raw).encode(), {
        "ETag": '"new"', "Last-Modified": "Sun, 20 Jul 2026 10:01:00 GMT"
    })
    result = updater.fetch_channel_manifest(
        metadata(), store, open_response=opener(response, captured)
    )
    assert result.status == "available"
    assert captured["headers"]["If-None-Match"] == '"old"'
    assert captured["headers"]["If-Modified-Since"].endswith("GMT")
    cached = updater.fetch_channel_manifest(
        metadata(), store, open_response=opener(FakeResponse(304))
    )
    assert cached.status == "not_modified"
    assert cached.manifest.version == "1.0.2"


def test_fetch_rejects_redirect_and_oversized_manifest(tmp_path):
    store = updater.UpdateStateStore(tmp_path / "state.json")
    with pytest.raises(updater.ManifestError, match="redirect_rejected"):
        updater.fetch_channel_manifest(metadata(), store, open_response=opener(FakeResponse(302)))
    payload = b"{" + b" " * updater.MANIFEST_MAX_BYTES + b"}"
    with pytest.raises(updater.ManifestError, match="manifest_too_large"):
        updater.fetch_channel_manifest(metadata(), store, open_response=opener(FakeResponse(200, payload)))


def test_streamed_download_atomic_rename_and_progress(tmp_path):
    data = os.urandom(updater.DOWNLOAD_CHUNK_BYTES * 2 + 31)
    manifest = parse_manifest(data)
    calls = []
    response = FakeResponse(200, data, {"Content-Length": str(len(data))})
    path = updater.download_installer(
        manifest, updates_dir=tmp_path,
        progress=lambda done, total: calls.append((done, total)),
        open_response=opener(response),
    )
    assert path.name == "1.0.2-OSIA-Setup.exe"
    assert path.read_bytes() == data
    assert not (tmp_path / "1.0.2.part").exists()
    assert len(calls) >= 3 and calls[-1] == (len(data), len(data))


@pytest.mark.parametrize(
    ("headers", "payload", "code"),
    [
        ({}, b"installer", "content_length_required"),
        ({"Content-Length": "8"}, b"installer", "content_length_mismatch"),
        ({"Content-Length": "9"}, b"short", "byte_count_mismatch"),
    ],
)
def test_download_size_failures_remove_partial(tmp_path, headers, payload, code):
    manifest = parse_manifest()
    with pytest.raises(updater.DownloadError, match=code):
        updater.download_installer(
            manifest, updates_dir=tmp_path,
            open_response=opener(FakeResponse(200, payload, headers)),
        )
    assert not (tmp_path / "1.0.2.part").exists()


def test_interrupted_and_hash_mismatch_downloads_remove_partial(tmp_path):
    data = b"installer"
    manifest = parse_manifest(data)
    with pytest.raises(updater.DownloadError, match="download_interrupted"):
        updater.download_installer(
            manifest, updates_dir=tmp_path,
            open_response=opener(FakeResponse(
                200, data, {"Content-Length": str(len(data))}, fail_after=1
            )),
        )
    bad = FakeResponse(200, b"different", {"Content-Length": str(len(data))})
    with pytest.raises(updater.DownloadError, match="sha256_mismatch"):
        updater.download_installer(manifest, updates_dir=tmp_path, open_response=opener(bad))
    assert not (tmp_path / "1.0.2.part").exists()


def test_cancelled_download_removes_partial(tmp_path):
    data = b"installer"
    manifest = parse_manifest(data)
    with pytest.raises(updater.DownloadCancelled, match="download_cancelled"):
        updater.download_installer(
            manifest, updates_dir=tmp_path, cancel_requested=lambda: True,
            open_response=opener(FakeResponse(200, data, {"Content-Length": "9"})),
        )
    assert not (tmp_path / "1.0.2.part").exists()


def test_cleanup_age_rules(tmp_path):
    old_part = tmp_path / "old.part"
    old_installer = tmp_path / "1.0.0-OSIA-Setup.exe"
    keep = tmp_path / "1.0.2-OSIA-Setup.exe"
    for path in (old_part, old_installer, keep):
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    updater.cleanup_updates(tmp_path, preserve_versions=("1.0.2",), now=10**9)
    assert not old_part.exists() and not old_installer.exists() and keep.exists()


def test_scheduler_single_flight_and_retry_sequence():
    scheduler = updater.UpdateScheduler(rng=lambda low, high: (low + high) / 2)
    assert scheduler.first_delay() == updater.FIRST_CHECK_SECONDS
    assert scheduler.begin() is True
    assert scheduler.begin() is False
    assert scheduler.complete(False) == updater.RETRY_DELAYS_SECONDS[0]
    assert scheduler.begin() is True
    assert scheduler.complete(False) == updater.RETRY_DELAYS_SECONDS[1]
    assert scheduler.begin() is True
    assert scheduler.complete(False) == updater.NORMAL_CHECK_SECONDS
    assert scheduler.begin() is True
    assert scheduler.complete(True) == updater.NORMAL_CHECK_SECONDS


def test_dismissal_manual_refresh_and_new_version(tmp_path):
    store = updater.UpdateStateStore(tmp_path / "state.json")
    assert updater.prepare_notification_state(store, "1.0.2", manual=False, now=100)
    assert updater.prepare_notification_state(store, "1.0.2", manual=False, now=101)
    updater.dismiss_update(store, "1.0.2", now=100)
    assert not updater.prepare_notification_state(store, "1.0.2", manual=False, now=101)
    assert updater.prepare_notification_state(
        store, "1.0.2", manual=False, now=100 + updater.DISMISS_SECONDS + 1
    )
    assert updater.prepare_notification_state(store, "1.0.2", manual=True, now=101)
    assert updater.prepare_notification_state(store, "1.0.3", manual=False, now=101)


def test_launch_plan_rehashes_uses_fixed_args_and_no_shell(tmp_path):
    data = b"installer"
    manifest = parse_manifest(data)
    path = updater.installer_path(manifest.version, tmp_path)
    path.write_bytes(data)
    store = updater.UpdateStateStore(tmp_path / "state.json")
    captured = {}

    def factory(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return object()

    updater.launch_update_installer(manifest, path, store, process_factory=factory)
    assert tuple(captured["command"][1:]) == updater.INSTALLER_ARGUMENTS
    assert captured["command"][1] == "/CURRENTUSER"
    assert captured["kwargs"]["shell"] is False
    assert not any(arg.startswith(("/DIR", "/VERYSILENT", "/FORCECLOSE")) for arg in captured["command"])
    assert store.load()["pending"]["version"] == "1.0.2"
    path.write_bytes(b"corrupt")
    with pytest.raises(updater.DownloadError, match="installer_reverification_failed"):
        updater.prepare_installer_plan(manifest, path)


def test_pending_success_and_failure(tmp_path):
    store = updater.UpdateStateStore(tmp_path / "state.json")
    store.update(pending={"version": "1.0.2"}, notifiedVersion="1.0.2")
    assert updater.reconcile_pending_update(metadata("1.0.2"), store) == "success"
    assert store.load()["pending"] is None
    store.update(pending={"version": "1.0.3"})
    assert updater.reconcile_pending_update(metadata("1.0.2"), store) == "failed"
    assert store.load()["lastError"] == "update_version_mismatch"


def test_updater_is_disabled_in_source_mode(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert updater.updater_enabled(metadata()) is False


def test_update_widgets_render_all_key_states(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from osia.gui import update_ui

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(update_ui, "load_app_metadata", lambda: (_ for _ in ()).throw(RuntimeError()))
    controller = update_ui.UpdateController(store=updater.UpdateStateStore(tmp_path / "state.json"))
    section = update_ui.UpdateSettingsSection(controller)
    banner = update_ui.UpdateBanner()
    for status in ("offline", "no_update", "available", "downloading", "verifying", "ready", "error"):
        state = controller.state
        state.update(
            status=status, availableVersion="1.0.2", releaseNotes=("Note",),
            progressReceived=5, progressTotal=10, error="fixture" if status == "error" else None,
            bannerVisible=status == "available",
        )
        section.apply_state(state)
        banner.apply_state(state)
        app.processEvents()
        assert status.replace("_", " ").split()[0].lower() in section.summary.text().lower() or section.summary.text()
    assert banner.label.text()


def test_update_check_is_off_gui_thread_and_manual_offline_is_visible(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from osia.gui import update_ui

    app = QApplication.instance() or QApplication([])
    current = metadata()
    monkeypatch.setattr(update_ui, "load_app_metadata", lambda: current)
    monkeypatch.setattr(update_ui, "updater_enabled", lambda _metadata: True)

    def slow_offline(_metadata, _store):
        time.sleep(0.15)
        raise updater.ManifestError("offline")

    monkeypatch.setattr(update_ui, "fetch_channel_manifest", slow_offline)
    controller = update_ui.UpdateController(
        store=updater.UpdateStateStore(tmp_path / "state.json"),
        scheduler=updater.UpdateScheduler(rng=lambda low, high: (low + high) / 2),
    )
    controller._timer.stop()
    started = time.monotonic()
    assert controller.check(manual=True)
    assert time.monotonic() - started < 0.1
    deadline = time.monotonic() + 2
    while controller.scheduler.in_flight and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert controller.state["status"] == "offline"
    assert controller.state["error"] == "offline"
    controller._timer.stop()
