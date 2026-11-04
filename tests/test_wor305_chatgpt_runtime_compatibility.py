"""Focused compatibility coverage for the local ChatGPT Account runtime."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from osia.chatgpt_account import app_server_client as app_server
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    RuntimeUnavailableError,
    _is_supported_runtime_version,
    _version_tuple,
)


@pytest.mark.parametrize("version", ["0.156.1", "0.166.0"])
def test_installed_codex_cli_version_metadata_is_parsed_and_accepted(version):
    metadata = f"codex-cli {version}"
    assert _version_tuple(metadata) == tuple(map(int, version.split(".")))
    client = AppServerClient(
        runtime_executable=Path("codex.exe"),
        runtime_version_override=metadata,
    )
    assert client._resolve_runtime() == (
        Path("codex.exe"), metadata,
    )


def test_existing_0144_compatibility_interval_remains_supported():
    assert _is_supported_runtime_version((0, 144, 1))
    assert _is_supported_runtime_version((0, 144, 9))


@pytest.mark.parametrize(
    "metadata",
    (
        "codex-cli 0.143.9",
        "codex-cli 0.145.0",
        "codex-cli 0.155.0-alpha.16.4",
        "codex-cli 0.156.2",
        "codex-cli 0.166.1",
    ),
)
def test_unreviewed_runtime_versions_remain_rejected(metadata):
    client = AppServerClient(
        runtime_executable=Path("codex.exe"),
        runtime_version_override=metadata,
    )

    with pytest.raises(RuntimeUnavailableError) as raised:
        client._resolve_runtime()

    assert raised.value.category == "runtime_incompatible"
    assert "incompatible with ChatGPT account support" in str(raised.value)


def test_malformed_version_metadata_is_not_normalized_to_a_release():
    assert _version_tuple("codex-cli development build") is None
    assert not _is_supported_runtime_version(None)


def test_source_runtime_staging_never_masks_missing_frozen_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(app_server, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(app_server, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(app_server.shutil, "which", lambda _: None)
    monkeypatch.delenv("OSIA_CODEX_EXECUTABLE", raising=False)
    staged = tmp_path / "build/codex-runtime/0.166.0/bin/codex.exe"

    monkeypatch.setattr(app_server, "is_frozen", lambda: False)
    assert staged in app_server._native_candidates()
    monkeypatch.setattr(app_server, "is_frozen", lambda: True)
    assert staged not in app_server._native_candidates()
