"""Packaged discovery integrity and credential-free native dependency probe."""
import os
import shutil
from pathlib import Path
import pytest
from osia.chatgpt_account import app_server_client as client

ROOT = Path(__file__).resolve().parents[1]


def test_frozen_runtime_never_falls_back_to_developer_path(monkeypatch, tmp_path):
    monkeypatch.setattr(client, 'is_frozen', lambda: True)
    monkeypatch.setenv('OSIA_CODEX_EXECUTABLE', str(tmp_path/'developer.exe'))
    monkeypatch.setattr(client.shutil, 'which', lambda name: pytest.fail('packaged runtime searched global PATH'))
    candidates = client._native_candidates(tmp_path)
    assert tmp_path/'developer.exe' not in candidates
    monkeypatch.setattr(client, '_probe_executable', lambda path: None)
    with pytest.raises(client.RuntimeUnavailableError, match='not installed'):
        client.AppServerClient(base_dir=tmp_path)._resolve_runtime()


def test_standalone_codex_license_is_in_release_inventory(monkeypatch, tmp_path):
    from test_windows_packaging import _load_windows_build
    build = _load_windows_build()
    monkeypatch.setattr(build, 'LEGAL_STAGE', tmp_path)
    monkeypatch.setattr(build.importlib.metadata, 'distributions', lambda: [])
    dependencies = build._stage_legal()
    assert dependencies == [{
        'name': 'OpenAI Codex', 'version': '0.144.1',
        'license': 'Apache-2.0', 'homepage': 'https://github.com/openai/codex',
    }]
    assert (tmp_path/'licenses/codex-0.144.1/LICENSE').read_bytes() == (ROOT/'packaging/windows/licenses/codex/LICENSE').read_bytes()
    assert (tmp_path/'licenses/codex-0.144.1/NOTICE').read_bytes() == (ROOT/'packaging/windows/licenses/codex/NOTICE').read_bytes()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows dependency probe')
def test_actual_bundled_runtime_starts_signed_out_with_empty_path(monkeypatch, tmp_path):
    bundle = ROOT/'build/windows/runtimes/codex'
    if not (bundle/'bin/codex.exe').is_file():
        pytest.skip('pinned standalone runtime not staged; do not provision in tests')
    # Copy only redistributable runtime files. Never read any operator auth home.
    package = tmp_path/'package'
    shutil.copytree(bundle, package/'runtimes/codex')
    monkeypatch.setenv('PATH', '')
    monkeypatch.delenv('OSIA_CODEX_EXECUTABLE', raising=False)
    monkeypatch.setattr(client, 'is_frozen', lambda: True)
    monkeypatch.setattr(client, 'resource_path', lambda *parts: package.joinpath(*parts))
    instance = client.AppServerClient(base_dir=tmp_path/'fresh-profile', compact_logs_on_startup=False)
    try:
        executable, version = instance._resolve_runtime()
        assert executable == package/'runtimes/codex/bin/codex.exe'
        assert '0.144.1' in version
        environment = instance.isolated_environment()
        assert environment['CODEX_HOME'] == str(instance.home_dir)
        assert not any(name in environment for name in ('OPENAI_API_KEY','CODEX_ACCESS_TOKEN','ACCESS_TOKEN'))
        assert instance.health_check()['healthy']
        assert instance.get_account_status()['connected'] is False
        instance.shutdown()
        assert instance.health_check()['healthy']
        assert instance.get_account_status()['connected'] is False
    finally:
        instance.shutdown()
