"""WOR-151 release-history and offline Settings regressions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from osia import i18n, updater
from osia.gui import update_ui
from osia.release_notes import (
    RELEASE_HISTORY,
    manifest_release_notes,
    normalize_remote_notes,
    release_for_version,
)


_QT_APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


def _metadata(version="1.0.5"):
    return updater.parse_app_metadata(json.dumps({
        "product": "OSIA",
        "version": version,
        "updateChannel": "internal-preview",
        "platform": "windows",
        "architecture": "x86_64",
        "buildTimestamp": "2026-07-20T10:00:00Z",
        "commitSha": "a" * 40,
        "updaterSchemaVersion": 1,
        "buildOverride": None,
    }))


def _manifest(version="1.0.6"):
    data = b"installer"
    return {
        "schemaVersion": 1,
        "product": "OSIA",
        "platform": "windows",
        "architecture": "x86_64",
        "channel": "internal-preview",
        "version": version,
        "publishedAt": "2026-07-21T10:01:00Z",
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
        "releaseNotes": ["**Clearer** update details."],
        "releaseNotesUrl": None,
        "rollout": {"state": "enabled"},
    }


def _controller(monkeypatch, tmp_path, *, cached=False):
    metadata = _metadata()
    store = updater.UpdateStateStore(tmp_path / "update-state.json")
    if cached:
        store.update(cachedManifest=_manifest())
    monkeypatch.setattr(update_ui, "load_app_metadata", lambda: metadata)
    monkeypatch.setattr(update_ui, "updater_enabled", lambda _value: True)
    controller = update_ui.UpdateController(store=store)
    controller._timer.stop()
    return controller


def test_embedded_notes_load_offline_and_are_newest_first():
    assert [entry.version for entry in RELEASE_HISTORY] == ["1.0.5", "1.0.4", "1.0.3"]
    current = release_for_version("1.0.5")
    assert current is not None and current.public
    assert "getting started" in " ".join(current.changes("en")).lower()
    assert "image attachments" in " ".join(current.changes("en")).lower()
    previous = release_for_version("1.0.4")
    assert previous is not None and previous.public
    assert "roll back" in " ".join(previous.changes("en")).lower()
    internal = release_for_version("1.0.3")
    assert internal is not None and not internal.public
    assert "superseded" in " ".join(internal.changes("en")).lower()
    assert manifest_release_notes("1.0.5") == list(current.changes_en)


def test_manifest_generator_reuses_embedded_source_and_rejects_user_marker():
    build = (ROOT / "packaging" / "windows" / "build_windows.py").read_text("utf-8")
    assert '"releaseNotes": manifest_release_notes(version)' in build
    assert '"onboarding.json"' in build


def test_cached_remote_notes_restore_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_ui,
        "fetch_channel_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network access during construction")
        ),
    )
    controller = _controller(monkeypatch, tmp_path, cached=True)
    assert controller.state["availableVersion"] == "1.0.6"
    assert controller.state["availablePublishedAt"] == "2026-07-21T10:01:00Z"
    assert controller.state["releaseNotes"] == ("**Clearer** update details.",)


def test_remote_markup_is_normalized_and_rendered_as_plain_text(
    monkeypatch, tmp_path,
):
    controller = _controller(monkeypatch, tmp_path)
    section = update_ui.UpdateSettingsSection(controller)
    state = controller.state
    state.update(
        status="available",
        availableVersion="1.0.6",
        availablePublishedAt="2026-07-21T10:01:00Z",
        releaseNotes=("&lt;b&gt;Safe&lt;/b&gt; **details**",),
    )
    section.apply_state(state)
    _QT_APP.processEvents()
    assert normalize_remote_notes(state["releaseNotes"]) == ("Safe details",)
    assert section.available_card.notes.textFormat() == Qt.TextFormat.PlainText
    assert "<b>" not in section.available_card.notes.text()
    assert "**" not in section.available_card.notes.text()
    section.deleteLater()
    _QT_APP.processEvents()


def test_installed_available_and_missing_notes_have_distinct_states(
    monkeypatch, tmp_path,
):
    controller = _controller(monkeypatch, tmp_path)
    section = update_ui.UpdateSettingsSection(controller)
    state = controller.state
    state.update(
        status="offline",
        installedVersion="9.9.9",
        availableVersion="10.0.0",
        releaseNotes=(),
    )
    section.apply_state(state)
    assert "9.9.9" in section.current_card.version.text()
    assert i18n.t("updates.notes_unavailable") in section.current_card.notes.text()
    assert "10.0.0" in section.available_card.version.text()
    assert i18n.t("updates.offline_installed_available") == section.empty_state.text()
    section.deleteLater()
    _QT_APP.processEvents()


def test_english_french_copy_and_long_text_stay_bounded(monkeypatch, tmp_path):
    controller = _controller(monkeypatch, tmp_path)
    section = update_ui.UpdateSettingsSection(controller)
    section.resize(390, 900)
    state = controller.state
    state.update(
        status="available", availableVersion="1.0.6",
        releaseNotes=("Long " * 45,),
    )
    try:
        i18n.set_language("en", persist=False)
        section.apply_state(state)
        assert section.current_card.badge.text() == "Current"
        assert "Getting Started" in section.current_card.notes.text()
        i18n.set_language("fr", persist=False)
        section.apply_state(state)
        assert section.current_card.badge.text() == "Actuelle"
        assert "premiers pas" in section.current_card.notes.text().lower()
        section.show()
        _QT_APP.processEvents()
        assert section.available_card.notes.wordWrap()
        assert section.available_card.width() <= section.width()
    finally:
        i18n.set_language("en", persist=False)
        section.close()
        section.deleteLater()
        _QT_APP.processEvents()
