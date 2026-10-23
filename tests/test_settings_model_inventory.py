"""Settings discovery must never initialize the recording stack."""
import json
import subprocess
import sys
from pathlib import Path

from osia.voice import model_inventory


def test_discovery_is_filesystem_only_in_fresh_process():
    root = Path(__file__).resolve().parents[1]
    script = "import sys,json; from osia.voice.model_inventory import available_whisper_model_sizes; print(json.dumps({'models':sorted(available_whisper_model_sizes()),'heavy':[n for n in ('scipy','numpy','sounddevice','faster_whisper','osia.voice.stt') if n in sys.modules]}))"
    result = subprocess.run([sys.executable, "-c", script], cwd=root, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["heavy"] == []


def test_discovery_preserves_bundled_and_cached_models(tmp_path, monkeypatch):
    hub = tmp_path / ".cache" / "huggingface" / "hub"
    hub.mkdir(parents=True)
    (hub / "models--Systran--faster-whisper-medium").mkdir()
    (hub / "unrelated").mkdir()
    monkeypatch.setattr(model_inventory.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(model_inventory, "bundled_model_path", lambda name: tmp_path if name == "faster-whisper-small" else None)
    assert model_inventory.available_whisper_model_sizes() == {"small", "medium"}
