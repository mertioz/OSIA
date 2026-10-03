"""Focused compatibility coverage for the local ChatGPT Account runtime."""

from pathlib import Path

import pytest

from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    RuntimeUnavailableError,
    _is_supported_runtime_version,
    _version_tuple,
)


def test_installed_codex_cli_version_metadata_is_parsed_and_accepted():
    version = _version_tuple("codex-cli 0.156.1")

    assert version == (0, 156, 1)
    client = AppServerClient(
        runtime_executable=Path("codex.exe"),
        runtime_version_override="codex-cli 0.156.1",
    )
    assert client._resolve_runtime() == (
        Path("codex.exe"), "codex-cli 0.156.1",
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
