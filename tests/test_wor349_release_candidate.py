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
