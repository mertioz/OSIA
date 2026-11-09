"""Desktop and server must preserve independent writes to a shared profile."""

import subprocess
import sys

import pytest

from osia import config


def test_two_processes_keep_both_settings(tmp_path, monkeypatch):
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
config.set_setting("tts_provider", "openai")
"""
    writer = None
    try:
        with config.edit_settings() as desktop:
            writer = subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert writer.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                writer.wait(timeout=0.25)
            desktop["time_format"] = "12h"
        _, err = writer.communicate(timeout=10)
        assert writer.returncode == 0, err
        final = config.load_settings()
        assert (final["time_format"], final["tts_provider"]) == ("12h", "openai")
    finally:
        if writer is not None and writer.poll() is None:
            writer.kill()
            writer.communicate()
