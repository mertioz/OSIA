"""Offline installed-release gates exercised by the frozen launcher too."""
from dataclasses import replace
import os

import pytest

from osia import i18n, updater
from osia.version import PRODUCT_VERSION, RELEASE_SEQUENCE
from test_windows_packaging import _load_launcher


@pytest.mark.parametrize("channel", ["stable", "internal-preview"])
@pytest.mark.parametrize("language", ["en", "fr"])
def test_frozen_release_gate_and_bilingual_beta_history(channel, language):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    metadata = updater.AppMetadata(
        product="OSIA", version=PRODUCT_VERSION, channel=channel,
        platform="windows", architecture="x86_64",
        build_timestamp="2026-10-20T00:00:00Z", commit_sha="a" * 40,
        updater_schema_version=2, build_override=None, release_sequence=RELEASE_SEQUENCE,
    )
    previous = i18n.get_language()
    try:
        i18n.set_language(language, persist=False)
        _load_launcher()._release_identity_smoke(metadata)
        with pytest.raises(RuntimeError, match="identity"):
            _load_launcher()._release_identity_smoke(replace(metadata, release_sequence=1))
    finally:
        i18n.set_language(previous, persist=False)
        app.processEvents()


def test_shipped_timezone_data_works_without_system_database(monkeypatch):
    import zoneinfo
    original = zoneinfo.TZPATH
    try:
        zoneinfo.reset_tzpath(())
        zoneinfo.ZoneInfo.clear_cache()
        _load_launcher()._timezone_smoke()
    finally:
        zoneinfo.ZoneInfo.clear_cache()
        zoneinfo.reset_tzpath(original)


def test_provider_probe_uses_canonical_chat_with_long_spaced_profile(monkeypatch, tmp_path):
    import tempfile
    from osia import config, execution_strategy, tool_attempts, turn_terminality
    from osia.provider_smoke import run_claude_messages_smoke

    temporary = tmp_path / "profil vierge é" / ("long-profile-" * 6)
    temporary.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(config, "get_setting", lambda name, default=None: default)
    monkeypatch.setattr(turn_terminality, "CONFIG_DIR", str(tmp_path / "turns"))
    store = tool_attempts.ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "get_tool_attempt_store", lambda: store)
    monkeypatch.setattr(
        execution_strategy, "build_execution_strategy",
        lambda *args, **kwargs: pytest.fail("probe entered the legacy strategy compiler"),
    )
    run_claude_messages_smoke()
