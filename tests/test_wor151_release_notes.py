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
from osia.version import display_historical_text
from osia.gui import update_ui
from osia.release_notes import (
    RELEASE_HISTORY,
    installer_release_notes,
    is_major_update,
    manifest_release_notes,
    normalize_remote_notes,
    release_for_version,
)


_QT_APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent


def _metadata(version="1.0.6"):
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


def _manifest(version="1.0.7"):
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
    assert [entry.version for entry in RELEASE_HISTORY] == [
        "0.9.259", "0.9.258", "0.9.257", "0.9.256", "0.9.255", "0.9.249", "0.9.248", "0.9.1", "0.9.0", "1.1.8", "1.1.7", "1.1.6", "1.1.5", "1.1.4", "1.1.3", "1.1.2", "1.1.1", "1.1.0", "1.0.6", "1.0.5", "1.0.4", "1.0.3",
    ]
    current = release_for_version("1.1.6")
    assert release_for_version("1.1.8").public is False
    assert current is not None and current.public
    assert not is_major_update(current.version)
    major = release_for_version("1.1.0")
    assert major is not None and major.public
    assert is_major_update(major.version)
    assert len(major.summary("en")) == len(major.summary("fr")) == 5
    assert len(major.changes("en")) == len(major.changes("fr")) >= 80
    assert len(major.sections) >= 20
    internal_current = release_for_version("1.0.6")
    assert internal_current is not None and not internal_current.public
    previous = release_for_version("1.0.5")
    assert previous is not None and previous.public
    assert "getting started" in " ".join(previous.changes("en")).lower()
    internal = release_for_version("1.0.3")
    assert internal is not None and not internal.public
    assert "superseded" in " ".join(internal.changes("en")).lower()
    assert manifest_release_notes("1.0.6") == list(internal_current.changes_en)


def test_117_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.7")
    assert current is not None and current.public
    assert manifest_release_notes("1.1.7") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.7"))
    assert len(current.sections) == 4
    assert len(current.changes("en")) == len(current.changes("fr")) == 9

    english = installer_release_notes("1.1.7", "en").lower()
    french = installer_release_notes("1.1.7", "fr").lower()
    for subject in (
        "same durable chat turn",
        "outcome_unknown stays unknown",
        "screenshot → action → screenshot",
        "permission broker",
        "deepseek-flash",
        "legal bundle 2026-10-05.4",
        "windows 1.1.6 remains the previous public rollback",
        "no mobile release is included",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "même tour chat durable",
        "outcome_unknown reste inconnu",
        "capture → action → capture",
        "courtier de permissions",
        "deepseek-flash",
        "bundle juridique wor-8 approuvé 2026-10-05.4",
        "windows 1.1.6 reste le rollback public précédent",
        "aucune release mobile n'est incluse",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.7")
    raw["releaseNotes"] = manifest_release_notes("1.1.7")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.6"))
    assert parsed.version == "1.1.7"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.7"))

def test_116_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.6")
    assert current is not None and current.public
    assert manifest_release_notes("1.1.6") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.6"))
    assert len(current.sections) == 4
    assert len(current.changes("en")) == len(current.changes("fr")) == 8

    english = installer_release_notes("1.1.6", "en").lower()
    french = installer_release_notes("1.1.6", "fr").lower()
    for subject in (
        "slow tool can release the chat turn",
        "possible or unknown external effects are never replayed",
        "no longer draws an external operating-system shadow",
        "updater reports bytes received",
        "canonical target language",
        "windows 1.1.5 remains the verified immutable rollback",
        "includes no mobile publication",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "outil lent peut libérer le tour chat",
        "effets externes possibles ou inconnus ne sont jamais rejoués",
        "n’affiche plus d’ombre externe du système",
        "updater 1.1.6 affiche les octets reçus",
        "langue cible canonique",
        "windows 1.1.5 reste le rollback immuable vérifié",
        "n’inclut aucune publication mobile",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.6")
    raw["releaseNotes"] = manifest_release_notes("1.1.6")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.5"))
    assert parsed.version == "1.1.6"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.6"))


def test_115_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.5")
    assert current is not None and current.public
    assert manifest_release_notes("1.1.5") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.5"))
    assert len(current.sections) == 3
    assert len(current.changes("en")) == len(current.changes("fr")) == 9

    english = installer_release_notes("1.1.5", "en").lower()
    french = installer_release_notes("1.1.5", "fr").lower()
    for subject in (
        "chat activity is keyed only to its exact conversation, request, and turn",
        "autonomous abandon targets only the exact run",
        "possible or unknown external effect is never replayed",
        "windows 1.1.4 remains the validated rollback",
        "no mobile publication is part of this windows release",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "l’activité chat dépend uniquement de sa conversation, de sa requête et de son tour exacts",
        "abandon autonomous cible uniquement le run exact",
        "effet externe possible ou inconnu n’est jamais rejoué",
        "windows 1.1.4 reste le rollback validé",
        "aucune publication mobile ne fait partie de cette release windows",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.5")
    raw["releaseNotes"] = manifest_release_notes("1.1.5")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.4"))
    assert parsed.version == "1.1.5"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.5"))


def test_114_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.4")
    assert current is not None and current.public
    assert manifest_release_notes("1.1.4") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.4"))
    assert len(current.sections) == 3
    assert len(current.changes("en")) == len(current.changes("fr")) == 9

    english = installer_release_notes("1.1.4", "en").lower()
    french = installer_release_notes("1.1.4", "fr").lower()
    for subject in (
        "active conversation, turn, and mission",
        "normal chat and autonomous",
        "legacy textual cancellation",
        "do not create a provider turn",
        "does not publish a separate mobile release or change the mobile interface",
        "windows 1.1.3 remains the validated rollback",
        "this release is windows-only. the next mobile update will be published separately.",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "conversation, de tour et de mission actifs",
        "chat normal et autonomous",
        "annulation textuelle historique",
        "ne créent ni tour fournisseur",
        "ne publie pas de release mobile séparée et ne modifie pas l’interface mobile",
        "windows 1.1.3 reste le rollback validé",
        "cette publication concerne windows uniquement. la prochaine mise à jour mobile sera publiée séparément.",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.4")
    raw["releaseNotes"] = manifest_release_notes("1.1.4")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.3"))
    assert parsed.version == "1.1.4"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.4"))


def test_113_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.3")
    assert current is not None and current.public
    assert manifest_release_notes("1.1.3") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.3"))
    assert len(current.sections) == 4
    assert len(current.changes("en")) == len(current.changes("fr")) == 15

    english = installer_release_notes("1.1.3", "en").lower()
    french = installer_release_notes("1.1.3", "fr").lower()
    for subject in (
        "durable tool-attempt lifecycle",
        "same turn",
        "sleep and wake",
        "mobile reconnect",
        "chat-to-autonomous",
        "cancellation",
        "provider and model",
        "no fallback",
        "windows 1.1.2 remains the validated rollback",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "cycle de vie durable des tentatives",
        "même tour",
        "veille et réveil",
        "reconnexion mobile",
        "chat vers autonomous",
        "annulation",
        "fournisseur et modèle",
        "aucun fallback",
        "windows 1.1.2 reste le rollback validé",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.3")
    raw["releaseNotes"] = manifest_release_notes("1.1.3")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.2"))
    assert parsed.version == "1.1.3"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.3"))


def test_112_hotfix_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.2")
    assert current is not None and current.public
    assert manifest_release_notes("1.1.2") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.2"))
    assert len(current.sections) == 3
    assert len(current.changes("en")) == len(current.changes("fr")) == 10

    english = installer_release_notes("1.1.2", "en").lower()
    french = installer_release_notes("1.1.2", "fr").lower()
    for subject in (
        "exact official stable endpoint",
        "download.osia.space",
        "/windows/<version>/osia-setup.exe",
        "content-length",
        "sha-256",
        "fully rehashed",
        "shell=false",
        "stable_notsigned",
        "unknown publisher",
        "do not automatically install 1.1.2",
        "windows 1.1.1 remains the validated rollback",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "endpoint stable officiel exact",
        "download.osia.space",
        "content-length",
        "sha-256",
        "entièrement rehashé",
        "shell=false",
        "stable_notsigned",
        "éditeur inconnu",
        "n’installent pas automatiquement 1.1.2",
        "windows 1.1.1 reste l’artefact de rollback validé",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.2")
    raw["releaseNotes"] = manifest_release_notes("1.1.2")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.1"))
    assert parsed.version == "1.1.2"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.2"))


def test_111_notes_are_complete_bilingual_and_manifest_compatible():
    current = release_for_version("1.1.1")
    assert current is not None
    assert manifest_release_notes("1.1.1") == list(current.summary_en)
    assert len(current.summary("en")) == len(current.summary("fr")) == 5
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.1"))
    assert len(current.sections) == 7
    assert len(current.changes("en")) == len(current.changes("fr")) == 26

    section_titles = " ".join(section.title_en for section in current.sections).lower()
    for subject in (
        "stable and preview",
        "skill activation",
        "language continuity",
        "chat to autonomous",
        "model and attachment",
        "windows controls",
        "distribution",
    ):
        assert subject in section_titles

    english = installer_release_notes("1.1.1", "en").lower()
    french = installer_release_notes("1.1.1", "fr").lower()
    for subject in (
        "internal-preview",
        "canonical revision",
        "per-turn language",
        "scheduled tasks",
        "planner, runner, verifier",
        "png, pdf, and docx",
        "android 1.1.0",
        "unknown publisher",
        "more info",
        "run anyway",
    ):
        assert display_historical_text(subject) in english
    for subject in (
        "stable et preview",
        "révision canonique",
        "continuité de langue",
        "scheduled tasks",
        "png, pdf et docx",
        "android 1.1.0",
        "éditeur inconnu",
        "informations complémentaires",
        "exécuter quand même",
    ):
        assert display_historical_text(subject) in french

    raw = _manifest(version="1.1.1")
    raw["releaseNotes"] = manifest_release_notes("1.1.1")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.1.0"))
    assert parsed.version == "1.1.1"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.1"))


def test_110_notes_are_exhaustive_bilingual_and_manifest_compatible():
    major = release_for_version("1.1.0")
    assert major is not None
    assert manifest_release_notes("1.1.0") == list(major.summary_en)
    assert all(len(note) <= 240 for note in manifest_release_notes("1.1.0"))

    section_text = (" ".join(section.title_en for section in major.sections) + " " + " ".join(major.changes("en"))).lower()
    for subject in (
        "core chat",
        "pc/mobile",
        "permissions",
        "deletion",
        "autonomous",
        "browser",
        "documents",
        "pdf",
        "image",
        "audio",
        "video",
        "voice",
        "provider",
        "chatgpt",
        "personality",
        "skills",
        "packaging",
        "android",
        "known limitations",
    ):
        assert subject in section_text

    english = installer_release_notes("1.1.0", "en").lower()
    french = installer_release_notes("1.1.0", "fr").lower()
    assert "windows 0.8.944" in english
    assert "android 0.8.3" in english
    assert "worker tooling" in english
    assert "not deployed" in english
    assert "windows 0.8.944" in french
    assert "android 0.8.3" in french
    assert "limites connues" in french
    assert "unknown publisher" in english
    assert "smartscreen" in english
    assert "more info" in english and "run anyway" in english
    assert "éditeur inconnu" in french
    assert "informations complémentaires" in french
    assert "exécuter quand même" in french


def test_generated_110_manifest_is_accepted_as_an_update_from_106():
    raw = _manifest(version="1.1.0")
    raw["releaseNotes"] = manifest_release_notes("1.1.0")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.0.6"))
    assert parsed.version == "1.1.0"
    assert parsed.release_notes == tuple(manifest_release_notes("1.1.0"))


def test_106_notes_are_bilingual_bounded_and_keep_internal_release_semantics():
    current = release_for_version("1.0.6")
    assert current is not None and not current.public
    assert len(current.changes_en) == len(current.changes_fr) == 3

    english = " ".join(current.changes_en).lower()
    for subject in (
        "native actions",
        "language of the current request",
        "without executing or retrying",
        "popups in real time",
        "returns to the opener",
        "long urls",
        "opaque identifiers",
        "without clipping",
        "copied text",
    ):
        assert display_historical_text(subject) in english

    french = " ".join(current.changes_fr).lower()
    for subject in (
        "actions natives",
        "langue de la demande actuelle",
        "sans les exécuter ni réessayer",
        "fenêtres en temps réel",
        "fenêtre d’origine",
        "url longues",
        "identifiants opaques",
        "sans rognage",
        "texte copié",
    ):
        assert display_historical_text(subject) in french


def test_105_notes_are_bilingual_bounded_and_keep_release_semantics():
    current = release_for_version("1.0.5")
    assert current is not None
    assert len(current.changes_en) <= 5
    assert len(current.changes_fr) <= 5
    assert len(current.changes_en) == len(current.changes_fr) == 5

    english = " ".join(current.changes_en).lower()
    for subject in (
        "essential getting started",
        "master osia",
        "advanced guide",
        "attach files before sending",
        "multiple files",
        "multimodal providers",
        "does not support vision",
        "update history offline",
        "close osia",
        "independent scrolling",
        "careful reasoning",
        "stability",
        "update",
        "windows distribution",
    ):
        assert display_historical_text(subject) in english

    french = " ".join(current.changes_fr).lower()
    for subject in (
        "démarrage essentiel",
        "maîtriser osia",
        "guide avancé",
        "joignez des fichiers avant l’envoi",
        "plusieurs fichiers",
        "fournisseurs multimodaux",
        "prend pas en charge la vision",
        "historique des mises à jour hors ligne",
        "fermez osia",
        "défilements indépendants",
        "raisonnement attentif",
        "stabilité",
        "mise à jour",
        "distribution windows",
    ):
        assert display_historical_text(subject) in french


def test_generated_105_manifest_is_accepted_as_an_update_from_104():
    raw = _manifest(version="1.0.5")
    raw["releaseNotes"] = manifest_release_notes("1.0.5")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.0.4"))
    assert parsed.version == "1.0.5"
    assert parsed.release_notes == tuple(manifest_release_notes("1.0.5"))


def test_generated_106_manifest_is_accepted_as_an_update_from_105():
    raw = _manifest(version="1.0.6")
    raw["releaseNotes"] = manifest_release_notes("1.0.6")
    parsed = updater.parse_manifest(json.dumps(raw), _metadata("1.0.5"))
    assert parsed.version == "1.0.6"
    assert parsed.release_notes == tuple(manifest_release_notes("1.0.6"))


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
    assert controller.state["availableVersion"] == "1.0.7"
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
        availableVersion="1.0.7",
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
        status="available", availableVersion="1.0.7",
        releaseNotes=("Long " * 45,),
    )
    try:
        i18n.set_language("en", persist=False)
        section.apply_state(state)
        assert section.current_card.badge.text() == "Current"
        assert "native actions" in section.current_card.notes.text().lower()
        i18n.set_language("fr", persist=False)
        section.apply_state(state)
        assert section.current_card.badge.text() == "Actuelle"
        assert "actions natives" in section.current_card.notes.text().lower()
        section.show()
        _QT_APP.processEvents()
        assert section.available_card.notes.wordWrap()
        assert section.available_card.width() <= section.width()
    finally:
        i18n.set_language("en", persist=False)
        section.close()
        section.deleteLater()
        _QT_APP.processEvents()


def test_major_update_card_is_distinct_scrollable_and_reduced_motion_safe(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    controller = _controller(monkeypatch, tmp_path)
    section = update_ui.UpdateSettingsSection(controller)
    section.resize(420, 900)
    state = controller.state
    state.update(
        status="downloading",
        installedVersion="1.0.6",
        availableVersion="1.1.0",
        availablePublishedAt="2026-08-02T10:00:00Z",
        releaseNotes=manifest_release_notes("1.1.0"),
        majorUpdate=True,
        progressReceived=3,
        progressTotal=4,
    )
    try:
        section.apply_state(state)
        section.show()
        _QT_APP.processEvents()
        assert section.available_card.objectName() == "majorUpdateCard"
        assert section.available_card.badge.text() == "Major update"
        assert "KNOWN LIMITATIONS" in section.available_card.notes.text()
        assert section.available_card.notes_scroll.height() <= 390
        assert section.available_card._reduced_motion
        assert section.available_card._animation.state().value == 0
        assert section.progress._accent == "major"
        assert section.progress._reduced_motion
        assert section.progress.value() == 750
    finally:
        section.close()
        section.deleteLater()
        _QT_APP.processEvents()


def test_public_beta_uses_its_own_accent_on_cards_banner_and_progress(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("OSIA_REDUCED_MOTION", "1")
    controller = _controller(monkeypatch, tmp_path)
    section = update_ui.UpdateSettingsSection(controller)
    banner = update_ui.UpdateBanner()
    state = controller.state
    state.update(status="available", availableVersion=update_ui.PRODUCT_VERSION, bannerVisible=True)
    try:
        section.apply_state(state)
        banner.apply_state(state)
        assert banner.objectName() == "publicBetaUpdateBanner"
        assert section.available_card.objectName() == "publicBetaUpdateCard"
        assert section.available_card.badge.objectName() == "publicBetaReleaseBadge"
        assert section.progress._accent == "beta"
        assert section.history_layout.itemAt(0).widget().objectName() == "publicBetaUpdateCard"

        state.update(installedVersion=update_ui.PRODUCT_VERSION, availableVersion="1.1.0")
        section.apply_state(state)
        banner.apply_state(state)
        assert section.current_card.objectName() == "publicBetaUpdateCard"
        assert section.available_card.objectName() == "majorUpdateCard"
        assert section.available_card.badge.objectName() == "releaseBadge"
        assert banner.objectName() == "majorUpdateBanner"
        assert section.progress._accent == "major"
    finally:
        banner.close()
        banner.deleteLater()
        section.close()
        section.deleteLater()
        _QT_APP.processEvents()
