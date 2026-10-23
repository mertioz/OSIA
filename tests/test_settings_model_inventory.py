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


def test_pending_recovery_does_not_hold_server_startup_or_health(monkeypatch):
    import asyncio
    from osia.server import server

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def pending_recovery():
            calls.append("recovery")
            started.set()
            await release.wait()

        monkeypatch.setattr(server, "_recover_tool_attempts_at_startup", pending_recovery)
        monkeypatch.setattr(server, "_TOOL_ATTEMPT_RECOVERY_TASK", None)
        await server._start_tool_attempt_recovery()
        await started.wait()
        task = server._TOOL_ATTEMPT_RECOVERY_TASK
        assert not task.done()
        assert (await server.root())["status"] == "ok"
        await server._start_tool_attempt_recovery()
        assert server._TOOL_ATTEMPT_RECOVERY_TASK is task
        assert calls == ["recovery"]
        await server._stop_tool_attempt_recovery()
        assert task.cancelled()
        assert server._TOOL_ATTEMPT_RECOVERY_TASK is None

    asyncio.run(scenario())
