"""WOR-170 Stable/Preview preference, selection, migration, and UI tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time

import pytest

from osia import i18n, updater


def _metadata(version="1.1.0", channel="stable"):
    return updater.parse_app_metadata(json.dumps({
        "product": "OSIA",
        "version": version,
        "updateChannel": channel,
        "platform": "windows",
        "architecture": "x86_64",
        "buildTimestamp": "2026-08-02T22:00:00Z",
        "commitSha": "a" * 40,
        "updaterSchemaVersion": 1,
        "buildOverride": None,
    }))


def _manifest(version, channel, *, rollout="enabled"):
    data = f"installer-{version}".encode()
    signed = channel == "stable"
    return {
        "schemaVersion": 1,
        "product": "OSIA",
        "platform": "windows",
        "architecture": "x86_64",
        "channel": channel,
        "version": version,
        "publishedAt": "2026-08-03T10:00:00Z",
        "installer": {
            "url": f"https://download.osia.space/windows/{version}/OSIA-Setup.exe",
            "sizeBytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "signed": signed,
            "signaturePublisher": "OSIA" if signed else None,
        },
        "mandatory": False,
        "minimumSupportedVersion": "1.0.1",
        "minimumUpdaterVersion": "1.0.1",
        "releaseNotes": [f"OSIA {version}."],
        "releaseNotesUrl": None,
        "rollout": {"state": rollout},
    }


class _Response:
    def __init__(self, status, payload=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._stream = io.BytesIO(payload)

    def read(self, amount=-1):
        return self._stream.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Routes:
    def __init__(self, **routes):
        self.routes = {channel: list(values) for channel, values in routes.items()}
        self.calls = []

    def __call__(self, url, headers, _connect_timeout, _read_timeout):
        channel = "internal-preview" if "/internal-preview/" in url else "stable"
        self.calls.append((channel, dict(headers)))
        value = self.routes[channel].pop(0)
        if isinstance(value, BaseException):
            raise value
        status, payload, response_headers = value
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        return _Response(status, payload, response_headers)


def _ok(payload, headers=None):
    return (200, payload, headers or {})


def test_preview_preference_migration_and_restart_persistence(tmp_path):
    new_store = updater.UpdateStateStore(tmp_path / "new.json")
    assert updater.initialize_preview_opt_in(
        _metadata(channel="internal-preview"), new_store
    ) is False
    assert new_store.load()["preview_opt_in"] is False

    stable_store = updater.UpdateStateStore(tmp_path / "stable.json")
    stable_store.update(cachedManifest=_manifest("1.1.1", "stable"))
    assert updater.initialize_preview_opt_in(
        _metadata(channel="internal-preview"), stable_store
    ) is False

    no_channel_store = updater.UpdateStateStore(tmp_path / "no-channel.json")
    no_channel_store.update(lastCheckedAt=1)
    assert updater.initialize_preview_opt_in(
        _metadata(channel="stable"), no_channel_store
    ) is False

    preview_store = updater.UpdateStateStore(tmp_path / "preview.json")
    preview_store.update(cachedManifest=_manifest("1.1.1", "internal-preview"))
    assert updater.initialize_preview_opt_in(
        _metadata(channel="stable"), preview_store
    ) is True
    assert updater.initialize_preview_opt_in(
        _metadata(channel="stable"), updater.UpdateStateStore(preview_store.path)
    ) is True

    legacy_preview = updater.UpdateStateStore(tmp_path / "legacy-preview.json")
    legacy_preview.update(lastCheckedAt=1)
    assert updater.initialize_preview_opt_in(
        _metadata(channel="internal-preview"), legacy_preview
    ) is True


def test_stable_only_consults_only_stable(tmp_path):
    routes = _Routes(stable=[_ok(_manifest("1.1.1", "stable"))])
    result = updater.fetch_eligible_manifest(
        _metadata(),
        updater.UpdateStateStore(tmp_path / "state.json"),
        preview_opt_in=False,
        open_response=routes,
    )
    assert result.manifest is not None
    assert (result.manifest.version, result.manifest.channel) == ("1.1.1", "stable")
    assert [channel for channel, _headers in routes.calls] == ["stable"]


@pytest.mark.parametrize(
    ("stable_version", "preview_version", "expected_channel"),
    [
        ("1.1.2", "1.1.1", "stable"),
        ("1.1.1", "1.1.2", "internal-preview"),
        ("1.1.2", "1.1.0", "stable"),
        ("1.1.0", "1.1.2", "internal-preview"),
    ],
)
def test_preview_mode_selects_authoritative_newest_version(
    tmp_path, stable_version, preview_version, expected_channel
):
    routes = _Routes(
        stable=[_ok(_manifest(stable_version, "stable"))],
        **{
            "internal-preview": [
                _ok(_manifest(preview_version, "internal-preview"))
            ]
        },
    )
    result = updater.fetch_eligible_manifest(
        _metadata(),
        updater.UpdateStateStore(tmp_path / "state.json"),
        preview_opt_in=True,
        open_response=routes,
    )
    assert result.manifest is not None
    assert result.manifest.channel == expected_channel
    assert result.manifest.version == max(stable_version, preview_version)
    assert [channel for channel, _headers in routes.calls] == [
        "stable", "internal-preview"
    ]


@pytest.mark.parametrize(
    "preview_response",
    [
        (404, b"", {}),
        OSError("preview offline"),
        _ok(b"{}"),
        _ok(_manifest("1.1.2", "internal-preview", rollout="paused")),
    ],
)
def test_unavailable_invalid_or_paused_preview_never_blocks_stable(
    tmp_path, preview_response
):
    routes = _Routes(
        stable=[_ok(_manifest("1.1.1", "stable"))],
        **{"internal-preview": [preview_response]},
    )
    result = updater.fetch_eligible_manifest(
        _metadata(),
        updater.UpdateStateStore(tmp_path / "state.json"),
        preview_opt_in=True,
        open_response=routes,
    )
    assert result.status == "available"
    assert result.manifest is not None
    assert result.manifest.channel == "stable"


def test_per_channel_etag_cache_survives_304_and_reconnect(tmp_path):
    routes = _Routes(
        stable=[
            _ok(_manifest("1.1.1", "stable"), {"ETag": '"stable-1"'}),
            (304, b"", {}),
        ],
        **{
            "internal-preview": [
                _ok(
                    _manifest("1.1.2", "internal-preview"),
                    {"ETag": '"preview-1"'},
                ),
                (304, b"", {}),
            ]
        },
    )
    store = updater.UpdateStateStore(tmp_path / "state.json")
    first = updater.fetch_eligible_manifest(
        _metadata(), store, preview_opt_in=True, open_response=routes
    )
    second = updater.fetch_eligible_manifest(
        _metadata(), store, preview_opt_in=True, open_response=routes
    )
    assert first.manifest is not None and first.manifest.version == "1.1.2"
    assert second.manifest is not None and second.manifest.version == "1.1.2"
    assert routes.calls[2][1]["If-None-Match"] == '"stable-1"'
    assert routes.calls[3][1]["If-None-Match"] == '"preview-1"'

    reconnect = _Routes(
        stable=[OSError("offline"), _ok(_manifest("1.1.1", "stable"))]
    )
    reconnect_store = updater.UpdateStateStore(tmp_path / "reconnect.json")
    with pytest.raises(updater.ManifestError, match="offline"):
        updater.fetch_eligible_manifest(
            _metadata(),
            reconnect_store,
            preview_opt_in=False,
            open_response=reconnect,
        )
    recovered = updater.fetch_eligible_manifest(
        _metadata(),
        reconnect_store,
        preview_opt_in=False,
        open_response=reconnect,
    )
    assert recovered.manifest is not None
    assert recovered.manifest.channel == "stable"


def test_disabling_preview_keeps_installed_version_and_checks_stable_only(tmp_path):
    store = updater.UpdateStateStore(tmp_path / "state.json")
    store.update(
        preview_opt_in=True,
        cachedManifest=_manifest("1.2.0", "internal-preview"),
        pending={"version": "1.2.1"},
    )
    updater.set_preview_opt_in(store, False)
    state = store.load()
    assert state["preview_opt_in"] is False
    assert state["cachedManifest"] is None
    assert state["pending"] == {"version": "1.2.1"}

    routes = _Routes(stable=[_ok(_manifest("1.1.9", "stable"))])
    result = updater.fetch_eligible_manifest(
        _metadata("1.2.0", "internal-preview"),
        store,
        preview_opt_in=False,
        open_response=routes,
    )
    assert result.status == "no_update"
    assert result.manifest is None
    assert [channel for channel, _headers in routes.calls] == ["stable"]


def _wait_for_check(app, controller):
    deadline = time.monotonic() + 2
    while controller.scheduler.in_flight and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_bilingual_confirmation_is_complete_and_permanent_help_is_calm():
    original = i18n.get_language()
    try:
        i18n.set_language("en", persist=False)
        english = i18n.t("updates.preview.confirm_text").lower()
        assert all(term in english for term in (
            "before stable", "bugs", "regressions", "incomplete",
            "stable releases remain eligible", "never downgrades",
        ))
        assert "bugs" not in i18n.t("updates.preview.help.enabled").lower()

        i18n.set_language("fr", persist=False)
        french = i18n.t("updates.preview.confirm_text").lower()
        assert all(term in french for term in (
            "avant les versions stables", "bugs", "régressions", "incomplets",
            "versions stables restent éligibles", "retour automatique",
        ))
        assert "bugs" not in i18n.t("updates.preview.help.enabled").lower()
    finally:
        i18n.set_language(original, persist=False)


def test_ui_confirmation_cancel_enable_and_persistence(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from osia.gui import update_ui

    app = QApplication.instance() or QApplication([])
    current = _metadata()
    monkeypatch.setattr(update_ui, "load_app_metadata", lambda: current)
    monkeypatch.setattr(update_ui, "updater_enabled", lambda _metadata: True)
    monkeypatch.setattr(
        update_ui,
        "fetch_eligible_manifest",
        lambda *_args, **_kwargs: updater.ManifestFetchResult("no_update", None),
    )
    store = updater.UpdateStateStore(tmp_path / "state.json")
    controller = update_ui.UpdateController(store=store)
    controller._timer.stop()
    section = update_ui.UpdateSettingsSection(controller)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel,
    )
    section.preview_toggle.setChecked(True)
    assert controller.preview_opt_in is False
    assert store.load()["preview_opt_in"] is False
    assert section.preview_mode.text() == i18n.t("updates.preview.mode.stable")

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    section.preview_toggle.setChecked(True)
    _wait_for_check(app, controller)
    assert controller.preview_opt_in is True
    assert store.load()["preview_opt_in"] is True
    assert section.preview_mode.text() == i18n.t("updates.preview.mode.enabled")
    assert section.preview_help.wordWrap()
    assert section.preview_toggle.accessibleName() == section.preview_label.text()
    assert section.preview_toggle.accessibleDescription() == section.preview_help.text()

    restarted = update_ui.UpdateController(
        store=updater.UpdateStateStore(store.path)
    )
    restarted._timer.stop()
    assert restarted.preview_opt_in is True
    section.close()
    section.deleteLater()
    controller._timer.stop()
    app.processEvents()


def test_controller_disable_preview_manual_check_reports_no_newer_stable(
    monkeypatch, tmp_path
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from osia.gui import update_ui

    app = QApplication.instance() or QApplication([])
    current = _metadata("1.2.0", "internal-preview")
    store = updater.UpdateStateStore(tmp_path / "state.json")
    store.update(preview_opt_in=True)
    checked = []

    def no_update(_metadata_value, _store, *, preview_opt_in):
        checked.append(preview_opt_in)
        return updater.ManifestFetchResult("no_update", None)

    monkeypatch.setattr(update_ui, "load_app_metadata", lambda: current)
    monkeypatch.setattr(update_ui, "updater_enabled", lambda _metadata: True)
    monkeypatch.setattr(update_ui, "fetch_eligible_manifest", no_update)
    controller = update_ui.UpdateController(store=store)
    controller._timer.stop()
    assert controller.set_preview_opt_in(False)
    _wait_for_check(app, controller)
    assert checked == [False]
    assert controller.state["status"] == "no_update"
    assert controller.state["availableVersion"] is None
    assert controller.state["error"] is None
    controller._timer.stop()


def test_representative_frozen_windows_updater_gate(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater, "is_frozen", lambda: True)
    assert updater.updater_enabled(_metadata(channel="stable"))
    assert updater.updater_enabled(_metadata(channel="internal-preview"))
