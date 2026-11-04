"""The background updater must never select a partial or incompatible runtime."""

import hashlib
import io
import json
import tarfile
import threading

import pytest

from osia.chatgpt_account import app_server_client as app_server
from osia.chatgpt_account import runtime_update as updater


TARGET = "x86_64-pc-windows-msvc"
ARCHIVE = f"codex-package-{TARGET}.tar.gz"
VERSION = "0.167.0"
URL = f"https://github.com/openai/codex/releases/download/rust-v{VERSION}/{ARCHIVE}"


@pytest.mark.parametrize("change", [
    {}, {"prerelease": True}, {"draft": True}, {"tag_name": "rust-v0.167.0-alpha.1"},
    {"digest": None}, {"browser_download_url": "https://example.com/runtime.tar.gz"},
])
def test_release_requires_stable_tag_official_asset_and_digest(monkeypatch, change):
    asset = {
        "name": ARCHIVE, "state": "uploaded", "browser_download_url": URL,
        "digest": "sha256:" + "a" * 64, "size": 1024,
    }
    release = {"tag_name": f"rust-v{VERSION}", "draft": False, "prerelease": False}
    for key, value in change.items():
        (release if key in release else asset)[key] = value
    release["assets"] = [asset]
    monkeypatch.setattr(updater, "_open_release_url", lambda _: io.BytesIO(json.dumps(release).encode()))
    result = updater._latest_release(TARGET)
    assert result == (None if change else updater._Release(VERSION, TARGET, URL, "a" * 64, 1024))


def _archive_bytes(*, extra_path=None):
    metadata = {
        "layoutVersion": 1, "version": VERSION, "target": TARGET, "variant": "codex",
        "entrypoint": "bin/codex.exe", "execServer": "bin/codex-exec-server.exe",
        "resourcesDir": "codex-resources", "pathDir": "codex-path",
    }
    files = {
        "codex-package.json": json.dumps(metadata).encode(),
        "bin/codex.exe": b"test",
        "bin/codex-exec-server.exe": b"test",
        "bin/codex-code-mode-host.exe": b"test",
        "codex-path/rg.exe": b"test",
        "codex-resources/codex-command-runner.exe": b"test",
        "codex-resources/codex-windows-sandbox-setup.exe": b"test",
    }
    if extra_path:
        files[extra_path] = b"test"
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for path, value in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    return data.getvalue()


@pytest.mark.parametrize("fault", [None, "digest", "traversal", "incompatible", "stop"])
def test_install_is_atomic_and_keeps_the_previous_runtime(monkeypatch, tmp_path, fault):
    previous = tmp_path / "existing-codex"
    previous.touch()
    monkeypatch.delenv("OSIA_CODEX_EXECUTABLE", raising=False)
    monkeypatch.setattr(app_server, "_native_candidates", lambda _: [previous])
    monkeypatch.setattr(app_server, "sys", type("Platform", (), {"platform": "win32"}))
    monkeypatch.setattr(
        app_server, "_probe_executable",
        lambda path: "codex-cli 0.166.0" if path == previous else f"codex-cli {VERSION}",
    )
    active = app_server.AppServerClient(base_dir=tmp_path)
    assert active._resolve_runtime()[0] == previous

    data = _archive_bytes(extra_path="../escape" if fault == "traversal" else None)
    digest = hashlib.sha256(data).hexdigest() if fault != "digest" else "0" * 64
    release = updater._Release(VERSION, TARGET, URL, digest, len(data))
    monkeypatch.setattr(updater, "_open_release_url", lambda _: io.BytesIO(data))

    def probe(executable, version, scratch, stop):
        assert executable.name == "codex.exe" and version == VERSION
        assert app_server.AppServerClient(base_dir=tmp_path)._resolve_runtime()[0] == previous
        if fault == "incompatible":
            raise app_server.RuntimeUnavailableError("App Server rejected strict config")
        if fault == "stop":
            stop.set()
        return True

    monkeypatch.setattr(updater, "_verify_runtime", probe)
    root = app_server._updated_runtime_dir(tmp_path)
    root.mkdir(parents=True)
    if fault in {"digest", "traversal", "incompatible"}:
        with pytest.raises((ValueError, app_server.RuntimeUnavailableError)):
            updater._install_release(root, release, threading.Event())
    else:
        assert updater._install_release(root, release, threading.Event()) is (fault is None)

    assert active._resolve_runtime()[0] == previous
    fresh = app_server.AppServerClient(base_dir=tmp_path)
    expected = root / VERSION / "bin" / "codex.exe" if fault is None else previous
    assert fresh._resolve_runtime()[0] == expected
    assert fresh.get_runtime_version() == (f"codex-cli {VERSION}" if fault is None else "codex-cli 0.166.0")
    assert not list(root.glob(".download-*"))
    if fault is None:
        monkeypatch.setattr(app_server, "_probe_executable", lambda _: "codex-cli 0.166.0")
        assert app_server.AppServerClient(base_dir=tmp_path)._resolve_runtime()[0] == previous


@pytest.mark.parametrize("installed,expected", [
    ("codex-cli 0.156.1", True),
    (f"codex-cli {VERSION}", False),
    ("codex-cli 0.168.0", False),
])
def test_daily_check_survives_reopening_and_never_downgrades(monkeypatch, tmp_path, installed, expected):
    requests = []
    installs = []
    now = [1_000_000.0]
    release = updater._Release(VERSION, TARGET, URL, "a" * 64, 1024)

    def latest(target):
        requests.append(target)
        return release

    monkeypatch.setattr(updater.time, "time", lambda: now[0])
    monkeypatch.setattr(updater, "_latest_release", latest)
    monkeypatch.setattr(updater.AppServerClient, "get_runtime_version", lambda _: installed)
    monkeypatch.setattr(updater, "_install_release", lambda *args: installs.append(args) or True)
    assert updater._check_when_due(tmp_path, TARGET, threading.Event()) == updater._DAY
    now[0] += 100
    assert updater._check_when_due(tmp_path, TARGET, threading.Event()) == updater._DAY - 100
    assert requests == [TARGET]
    assert len(installs) == int(expected)
    now[0] += updater._DAY - 100
    updater._check_when_due(tmp_path, TARGET, threading.Event())
    assert requests == [TARGET, TARGET]
