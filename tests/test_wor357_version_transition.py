"""Version epoch migration preserves immutable artifact identity and trust."""
import json
from dataclasses import replace
from pathlib import Path
import pytest
from osia import updater
from osia.version import PRODUCT_VERSION, RELEASE_SEQUENCE, HISTORICAL_DISPLAY_VERSIONS, display_version, parse_product_version
from osia.release_notes import RELEASE_HISTORY
from test_updater import metadata, manifest_dict


def new_manifest(sequence=RELEASE_SEQUENCE, version=PRODUCT_VERSION):
    raw = manifest_dict(version=version)
    raw.update(schemaVersion=2, releaseSequence=sequence)
    return raw


@pytest.mark.parametrize('old', ['1.0.0','1.0.6','1.1.0','1.1.7','1.1.8'])
def test_legacy_identity_can_migrate_by_sequence_despite_lower_semver(old):
    result = updater.parse_manifest(json.dumps(new_manifest()), metadata(old))
    assert result.version == PRODUCT_VERSION
    assert result.release_sequence == RELEASE_SEQUENCE
    assert result.installer.url.endswith(f'/{PRODUCT_VERSION}/OSIA-Setup.exe')
    assert result.as_dict()['releaseSequence'] == RELEASE_SEQUENCE


def test_new_epoch_refuses_legacy_manifest_equal_sequence_and_wrong_channel():
    with pytest.raises(updater.ManifestError, match='release_sequence_required'):
        updater.parse_manifest(json.dumps(manifest_dict(version=PRODUCT_VERSION)), metadata('1.1.8'))
    installed = replace(metadata(PRODUCT_VERSION), updater_schema_version=2, release_sequence=RELEASE_SEQUENCE)
    with pytest.raises(updater.ManifestError, match='legacy_manifest_after_transition'):
        updater.parse_manifest(json.dumps(manifest_dict(version='1.1.8')), installed)
    with pytest.raises(updater.ManifestError, match='equal_version'):
        updater.parse_manifest(json.dumps(new_manifest()), installed)
    with pytest.raises(updater.ManifestError, match='downgrade'):
        updater.parse_manifest(json.dumps(new_manifest()), replace(installed, release_sequence=RELEASE_SEQUENCE+1))
    raw = new_manifest(RELEASE_SEQUENCE+1, '0.9.2')
    result = updater.parse_manifest(json.dumps(raw), installed)
    assert result.release_sequence == RELEASE_SEQUENCE+1
    raw['channel'] = 'stable'
    with pytest.raises(updater.ManifestError, match='channel_mismatch'):
        updater.parse_manifest(json.dumps(raw), installed)


@pytest.mark.parametrize('value', [True, 0, -1, '2026102001', 2147483648])
def test_bad_sequence_does_not_bypass_manifest_validation(value):
    with pytest.raises(updater.ManifestError, match='invalid_release_sequence'):
        updater.parse_manifest(json.dumps(new_manifest(value)), metadata('1.1.8'))


def test_history_is_display_only_and_official_source_leads_history():
    assert PRODUCT_VERSION == '0.9.259'
    assert display_version(PRODUCT_VERSION) == '0.9.259'
    assert display_version('0.9.248') == '0.9.0248'
    assert display_version('0.9.1') == '0.9.01'
    with pytest.raises(ValueError, match='invalid_product_version'):
        parse_product_version('0.9.0248')
    assert display_version('1.0.0') == '0.8.0'
    assert display_version('1.1.8') == '0.8.952'
    assert len(set(HISTORICAL_DISPLAY_VERSIONS.values())) == len(HISTORICAL_DISPLAY_VERSIONS)
    assert all(parse_product_version(v) < (0,9,0) for v in HISTORICAL_DISPLAY_VERSIONS.values())
    assert RELEASE_HISTORY[0].version == PRODUCT_VERSION
    old = next(r for r in RELEASE_HISTORY if r.version == '1.1.8')
    assert old.released_on == '2026-10-18'
    assert old.version == '1.1.8'


def test_unsigned_stable_successor_uses_the_validated_release_epoch():
    installed = replace(
        metadata('0.9.1', channel='stable'),
        updater_schema_version=2,
        release_sequence=2026110501,
    )
    source_url = updater.CHANNEL_URLS['stable']
    major, minor, patch = parse_product_version(PRODUCT_VERSION)
    next_version = f'{major}.{minor}.{patch + 1}'
    for version, sequence in ((PRODUCT_VERSION, RELEASE_SEQUENCE), (next_version, RELEASE_SEQUENCE + 1)):
        raw = new_manifest(sequence, version)
        raw['channel'] = 'stable'
        parsed = updater.parse_manifest(json.dumps(raw), installed, source_url=source_url)
        assert parsed.temporary_unsigned_stable
        with pytest.raises(updater.ManifestError, match='stable_unsigned'):
            updater.parse_manifest(json.dumps(raw), installed)
    below_floor = new_manifest(RELEASE_SEQUENCE + 1, '0.8.999')
    below_floor['channel'] = 'stable'
    with pytest.raises(updater.ManifestError, match='stable_unsigned'):
        updater.parse_manifest(json.dumps(below_floor), installed, source_url=source_url)


def test_same_version_repair_requires_the_exact_sequence_in_the_download_url(tmp_path):
    version = '0.9.248'
    repair_sequence = 2026110601
    installed = replace(metadata(version, 'stable'), updater_schema_version=2, release_sequence=2026110502)
    source_url = updater.CHANNEL_URLS['stable']
    raw = new_manifest(repair_sequence, version)
    raw['channel'] = 'stable'
    base = f'https://download.osia.space/windows/{version}'
    repair_url = f'{base}/builds/{repair_sequence}/OSIA-Setup.exe'
    raw['installer']['url'] = repair_url
    manifest = updater.parse_manifest(json.dumps(raw), installed, source_url=source_url)
    assert manifest.version == installed.version and manifest.installer.url == repair_url
    path = updater.installer_path(manifest.version, tmp_path)
    path.write_bytes(b'installer')
    store = updater.UpdateStateStore(tmp_path / 'update-state.json')
    updater.launch_update_installer(manifest, path, store, process_factory=lambda *_a, **_kw: object())
    assert updater.reconcile_pending_update(installed, store) == 'failed'
    updater.launch_update_installer(manifest, path, store, process_factory=lambda *_a, **_kw: object())
    assert updater.reconcile_pending_update(replace(installed, release_sequence=repair_sequence), store) == 'success'

    for url in (f'{base}/builds/2026110502/OSIA-Setup.exe', repair_url + '?next=1'):
        raw['installer']['url'] = url
        with pytest.raises(updater.ManifestError, match='invalid_installer_url'):
            updater.parse_manifest(json.dumps(raw), installed, source_url=source_url)


def test_installer_guards_both_interactive_and_update_mode_and_keeps_identity():
    script = (Path(__file__).resolve().parents[1]/'packaging/windows/OSIA.iss').read_text()
    assert "(not UpdateModeRequested) and (not RegKeyExists(HKCU, UninstallKey))" in script
    assert 'OSIAReleaseSequence' in script
    assert '{#ReleaseSequence} < ExistingSequence' in script
    assert '8B8666D0-0DD4-4D1C-86D0-6B5E23A4FE36' in script


def test_qt_history_uses_alias_and_keeps_original_in_details(monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    from osia.gui.update_ui import _ReleaseCard
    from osia.version import display_historical_text
    app = QApplication.instance() or QApplication([])
    card = _ReleaseCard()
    try:
        card.apply(version='1.1.8', released_on='2026-10-18', notes=('Stable 1.1.7 remains rollback.',))
        assert '0.8.952' in card.version.text()
        assert '1.1.8' in card.version.toolTip()
        assert '0.8.951' in card.notes.text()
        assert display_historical_text('Android 1.0.2; Windows 1.1.8') == 'Android 0.8.3; Windows 0.8.952'
        url = 'https://download.osia.space/windows/1.1.8/OSIA-Setup.exe'
        assert display_historical_text(url) == url
    finally:
        card.close()
        card.deleteLater()
        app.processEvents()
