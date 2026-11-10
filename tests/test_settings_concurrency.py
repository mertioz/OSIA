"""Desktop and server must preserve independent writes to a shared profile."""

import subprocess
import sys

import pytest

from osia import config


@pytest.mark.parametrize("nested_reasoning", [False, True])
def test_two_processes_keep_both_settings(tmp_path, monkeypatch, nested_reasoning):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    monkeypatch.setattr(config, "_ensure_config_dir", lambda: None)
    config.save_settings({"time_format": "24h", "tts_provider": "local"})
    script = """
import sys
from osia import config
config.SETTINGS_FILE = sys.argv[1]
config._ensure_config_dir = lambda: None
print("ready", flush=True)
if sys.argv[2] == "reasoning":
    config.set_model_reasoning_option("groq:mobile-model", "high")
else:
    config.set_setting("tts_provider", "openai")
"""
    writer = None
    try:
        with config.edit_settings() as desktop:
            writer = subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(path), "reasoning" if nested_reasoning else "setting"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert writer.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                writer.wait(timeout=0.25)
            if nested_reasoning:
                desktop["reasoning"]["models"]["openai:desktop-model"] = "low"
            else:
                desktop["time_format"] = "12h"
        _, err = writer.communicate(timeout=10)
        assert writer.returncode == 0, err
        final = config.load_settings()
        if nested_reasoning:
            assert final["reasoning"]["models"] == {
                "openai:desktop-model": "low", "groq:mobile-model": "high",
            }
        else:
            assert (final["time_format"], final["tts_provider"]) == ("12h", "openai")
    finally:
        if writer is not None and writer.poll() is None:
            writer.kill()
            writer.communicate()


def test_model_selection_broadcasts_one_complete_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(config, "_ensure_config_dir", lambda: None)
    seen = []

    def observe(key):
        settings = config.load_settings()
        seen.append((key, settings["selected_model"], settings["selected_model_provider"]))

    unsubscribe = config.add_setting_change_listener(observe)
    try:
        config.set_model_selection("gpt-6.1-sol", "openai")
        config.set_setting("selected_model", "inferred-provider-model")
        config.set_model_selection("another-inferred-model", None)
    finally:
        unsubscribe()

    assert seen == [
        ("selected_model", "gpt-6.1-sol", "openai"),
        ("selected_model", "inferred-provider-model", None),
        ("selected_model", "another-inferred-model", None),
    ]
