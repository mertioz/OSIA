"""Codex's managed home is usable on first launch and after a runtime update."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import pytest

from osia import image_inputs
from osia.chatgpt_account import app_server_client
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    RuntimeUnavailableError,
)


def _client_with_system_skill(tmp_path, name, monkeypatch):
    client = AppServerClient(
        base_dir=tmp_path / "codex",
        compact_logs_on_startup=False,
    )
    system_skills = client.home_dir / "skills" / ".system"
    (system_skills / name).mkdir(parents=True)
    (system_skills / ".codex-system-skills.marker").write_text(
        "test-system-skill-marker",
        encoding="utf-8",
    )
    monkeypatch.setattr(image_inputs, "cleanup_media_store", lambda: None)
    monkeypatch.setattr(
        image_inputs,
        "cleanup_owned_temp_images",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        client,
        "_drain_operational_image_logs",
        lambda **_kwargs: True,
    )
    return client


@pytest.mark.parametrize("name", ("review-agent", "create-statusline"))
def test_builtin_system_skills_do_not_block_runtime(
    tmp_path, monkeypatch, name,
):
    client = _client_with_system_skill(tmp_path, name, monkeypatch)

    client._ensure_layout()
    client._ensure_layout()

    assert (client.home_dir / "config.toml").is_file()


@pytest.mark.parametrize("name", ("future-system-skill", "custom-skill"))
def test_unrecognized_system_skills_still_block_runtime(
    tmp_path, monkeypatch, name,
):
    client = _client_with_system_skill(tmp_path, name, monkeypatch)

    with pytest.raises(RuntimeUnavailableError) as raised:
        client._ensure_layout()

    assert raised.value.category == "isolation_failed"
    assert "unexpected skills" in str(raised.value)


def test_config_bootstrap_checks_override_then_default_without_importing_credentials(
    tmp_path, monkeypatch,
):
    client = _client_with_system_skill(tmp_path, "create-statusline", monkeypatch)
    monkeypatch.setattr(app_server_client, "_runtime_base_dir", lambda: client.base_dir)
    user_home = tmp_path / "user"
    personal = user_home / ".codex"
    override = tmp_path / "custom-home"
    monkeypatch.setattr(Path, "home", lambda: user_home)
    monkeypatch.setenv("CODEX_HOME", str(override))
    for directory in (personal, override):
        directory.mkdir(parents=True)
    # An equivalent configuration may have different whitespace or comments.
    valid = app_server_client._SAFE_CONFIG + "\n# Existing compatible Codex settings\n"
    (personal / "config.toml").write_text(valid, encoding="utf-8")
    incompatible = app_server_client._SAFE_CONFIG + '\n[mcp_servers.external]\ncommand = "external"\n'
    (override / "config.toml").write_text(incompatible, encoding="utf-8")
    (personal / "auth.json").write_text('{"fixture": true}', encoding="utf-8")

    client._ensure_layout()

    config = client.home_dir / "config.toml"
    assert config.read_text("utf-8") == valid
    assert (override / "config.toml").read_text("utf-8") == incompatible
    assert not (client.home_dir / "auth.json").exists()
    assert client.isolated_environment()["CODEX_HOME"] == str(client.home_dir)

    (override / "config.toml").write_text(app_server_client._SAFE_CONFIG, encoding="utf-8")
    client._ensure_layout()
    assert config.read_text("utf-8") == valid  # The existing OSIA profile takes precedence.
    config.unlink()
    client._ensure_layout()
    assert config.read_text("utf-8") == app_server_client._SAFE_CONFIG


def test_first_launch_creates_one_complete_config_for_concurrent_clients(tmp_path, monkeypatch):
    client = _client_with_system_skill(tmp_path, "create-statusline", monkeypatch)
    second = AppServerClient(base_dir=client.base_dir)
    sync = threading.Barrier(2, timeout=5)

    def first_launch():
        sync.wait()
        return app_server_client._SAFE_CONFIG

    monkeypatch.setattr(client, "_initial_config", first_launch)
    monkeypatch.setattr(second, "_initial_config", first_launch)
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(lambda item: item._ensure_codex_config(), (client, second)))
    assert (client.home_dir / "config.toml").read_text("utf-8") == app_server_client._SAFE_CONFIG


def test_legacy_config_migration_keeps_osia_account_storage(tmp_path, monkeypatch):
    client = _client_with_system_skill(tmp_path, "create-statusline", monkeypatch)
    config = client.home_dir / "config.toml"
    auth = client.home_dir / "auth.json"
    config.write_text(app_server_client._LEGACY_SAFE_CONFIG, encoding="utf-8")
    auth.write_text('{"fixture": true}', encoding="utf-8")
    client._ensure_layout()
    assert config.read_text("utf-8") == app_server_client._SAFE_CONFIG
    assert auth.read_text("utf-8") == '{"fixture": true}'
