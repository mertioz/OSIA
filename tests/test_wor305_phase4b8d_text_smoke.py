"""The current Codex system skill must not block the isolated account runtime."""

import pytest

from osia import image_inputs
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


def test_current_review_agent_system_skill_does_not_block_runtime(
    tmp_path, monkeypatch,
):
    client = _client_with_system_skill(tmp_path, "review-agent", monkeypatch)

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
