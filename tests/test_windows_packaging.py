"""Focused regression tests for the self-contained Windows distribution."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


ROOT = Path(__file__).resolve().parent.parent


def _load_launcher():
    path = ROOT / "packaging" / "windows" / "launcher.py"
    spec = importlib.util.spec_from_file_location("osia_packaging_launcher", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_runtime_source_paths_are_cwd_independent(monkeypatch, tmp_path):
    from osia import runtime

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)
    assert runtime.resource_root() == ROOT
    assert runtime.resource_path("assets", "osia_icon.png").is_file()
    assert runtime.application_dir() == ROOT


def test_runtime_frozen_paths_support_spaces_and_unicode(monkeypatch, tmp_path):
    from osia import runtime

    bundle = tmp_path / "Données OSIA" / "_internal"
    bundle.mkdir(parents=True)
    executable = tmp_path / "Données OSIA" / "OSIA.exe"
    executable.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert runtime.resource_root() == bundle
    assert runtime.application_dir() == executable.parent


def test_launcher_normalizes_frozen_execv_duplicate(monkeypatch, tmp_path):
    launcher = _load_launcher()
    executable = tmp_path / "OSIA install" / "OSIA.exe"
    monkeypatch.setattr(sys, "executable", str(executable))
    assert launcher._normalized_args([str(executable), "--osia-server"]) == [
        "--osia-server"
    ]


def test_packaged_task_action_uses_osia_executable(monkeypatch, tmp_path):
    from osia.utils import systemd_manager as manager

    app = tmp_path / "OSIA App" / "OSIA.exe"
    captured = {}
    monkeypatch.setattr(manager, "is_frozen", lambda: True)
    monkeypatch.setattr(manager, "_APP_EXE", app)
    monkeypatch.setattr(manager, "_APP_DIR", app.parent)
    monkeypatch.setattr(manager, "_win_delete_task", lambda _name: True)
    monkeypatch.setattr(manager, "_win_current_user", lambda: "DOMAIN\\User")

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_ps(_script, env, timeout=30.0):
        captured.update(env)
        return Result()

    monkeypatch.setattr(manager, "_ps", fake_ps)
    assert manager._win_register_task("server") is True
    assert captured["OSIA_PYW"] == str(app)
    assert captured["OSIA_ARG"] == "--osia-service server"
    assert captured["OSIA_REPO"] == str(app.parent)
    assert manager._win_register_task("tunnel") is False


def test_autostart_command_quotes_packaged_unicode_path(monkeypatch, tmp_path):
    from osia.utils import assets

    executable = tmp_path / "OSIA Élégant" / "OSIA.exe"
    monkeypatch.setattr(assets, "is_frozen", lambda: True)
    monkeypatch.setattr(assets, "application_executable", lambda: executable)
    assert assets._windows_autostart_command() == f'"{executable}" --wake'


def test_wav_to_mp3_uses_in_process_encoder():
    import av
    from osia.server.server import _wav_to_mp3

    samples = np.sin(np.arange(2400) * 2 * np.pi * 440 / 24000).astype("float32")
    wav = io.BytesIO()
    sf.write(wav, samples, 24000, format="WAV")
    encoded = _wav_to_mp3(wav.getvalue())
    assert encoded.startswith(b"ID3")
    decoded = av.open(io.BytesIO(encoded), mode="r")
    assert sum(frame.samples for frame in decoded.decode(audio=0)) >= len(samples)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path adapter")
def test_espeak_adapter_handles_unicode_install_path(tmp_path):
    from osia.voice.tts import _patch_windows_espeak_data_path
    from phonemizer.backend.espeak.api import EspeakAPI
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    import espeakng_loader

    unicode_root = tmp_path / "Données OSIA"
    shutil.copytree(Path(espeakng_loader.get_data_path()), unicode_root / "data")
    shutil.copy2(espeakng_loader.get_library_path(), unicode_root / "espeak-ng.dll")
    original_property = EspeakWrapper.data_path
    original_library = EspeakWrapper._ESPEAK_LIBRARY
    original_data = EspeakWrapper._ESPEAK_DATA_PATH
    try:
        EspeakWrapper.set_library(str(unicode_root / "espeak-ng.dll"))
        EspeakWrapper.set_data_path(str(unicode_root / "data"))
        _patch_windows_espeak_data_path()
        wrapper = EspeakWrapper()
        assert wrapper.version >= (1, 52, 0)
        assert Path(wrapper.data_path).is_dir()
        assert isinstance(wrapper._espeak, EspeakAPI)
    finally:
        EspeakWrapper.data_path = original_property
        if hasattr(EspeakWrapper, "_osia_data_path_getter"):
            delattr(EspeakWrapper, "_osia_data_path_getter")
        EspeakWrapper.set_library(original_library)
        EspeakWrapper.set_data_path(original_data)


def test_frozen_stt_falls_back_to_bundled_small(monkeypatch, tmp_path):
    from osia.voice import stt

    bundled = tmp_path / "faster-whisper-small"
    bundled.mkdir()
    calls = []

    class FakeWhisperModel:
        def __init__(self, model_ref, **kwargs):
            calls.append((model_ref, kwargs))

    monkeypatch.setattr(stt, "_model", None)
    monkeypatch.setattr(stt, "_model_size", None)
    monkeypatch.setattr(stt, "is_frozen", lambda: True)
    monkeypatch.setattr(
        stt,
        "bundled_model_path",
        lambda name: bundled if name == "faster-whisper-small" else None,
    )
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )

    model, size = stt._get_model("medium")
    assert isinstance(model, FakeWhisperModel)
    assert size == "small"
    assert calls == [
        (
            str(bundled),
            {"device": "cpu", "compute_type": "int8", "local_files_only": True},
        )
    ]


def test_installer_contract_preserves_user_data_and_has_real_progress():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "DefaultDirName={localappdata}\\Programs\\OSIA" in script
    assert "PrivilegesRequired=lowest" in script
    assert "CurInstallProgressChanged" in script
    assert "PrepareToInstall" in script
    assert "DeinitializeSetup" in script
    assert "--osia-remove-integration" in script
    assert 'Tasks: firewall' in script
    assert "{param:NOINTEGRATION|0}" in script
    assert script.count("Check: ShouldRemoveIntegration") == 2
    assert ".osia-no-integration-test" in script
    assert "Result := not IntegrationSuppressed" in script
    assert "OutputBaseFilename=OSIA-Setup" in script
    assert "{userappdata}\\.osia" not in script.lower()
    assert "Path.home()" not in script


def test_installer_ui_uses_explicit_accessible_osia_palette():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    for color in (
        "OSIA_MAIN_BACKGROUND = $00120B0D",
        "OSIA_ELEVATED_SURFACE = $00211217",
        "OSIA_SECONDARY_SURFACE = $002F1821",
        "OSIA_ACCENT = $00F5B5C4",
        "OSIA_PRIMARY_TEXT = $00FFF6F8",
        "OSIA_SECONDARY_TEXT = $00E7CED5",
        "OSIA_HELPER_TEXT = $00C2A4AD",
        "OSIA_DISABLED_TEXT = $00907981",
        "OSIA_SUCCESS_TEXT = $00C1E3A7",
        "OSIA_ERROR_TEXT = $00A7A7F3",
        "OSIA_WARNING_TEXT = $007DC2F1",
    ):
        assert color in script

    # WizardBackColor also affects pre-wizard native task dialogs, whose text
    # follows the Windows theme. Only the main wizard receives the dark skin.
    assert "WizardBackColor=" not in script
    for control in (
        "WizardForm.WelcomeLabel1",
        "WizardForm.WelcomeLabel2",
        "WizardForm.PageNameLabel",
        "WizardForm.PageDescriptionLabel",
        "WizardForm.SelectDirLabel",
        "WizardForm.SelectDirBrowseLabel",
        "WizardForm.DiskSpaceLabel",
        "WizardForm.SelectTasksLabel",
        "WizardForm.ReadyLabel",
        "WizardForm.PreparingLabel",
        "WizardForm.StatusLabel",
        "WizardForm.FilenameLabel",
        "WizardForm.FinishedHeadingLabel",
        "WizardForm.FinishedLabel",
    ):
        assert f"StyleLabel({control}" in script
    assert "StyleDarkList(WizardForm.TasksList)" in script
    assert "StyleDarkList(WizardForm.RunList)" in script
    assert "StyleDarkMemo(WizardForm.ReadyMemo)" in script
    assert "StyleDarkMemo(DetailsMemo)" in script


def test_installer_ui_keeps_native_accessibility_and_shows_real_state():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "WizardStyle=modern dynamic hidebevels" in script
    assert "WizardSizePercent=115" in script
    assert "WizardForm.WizardBitmapImage.Visible := False" in script
    assert "WizardForm.WizardBitmapImage2.Visible := False" in script
    assert "WizardForm.NextButton.Default := True" in script
    assert "WizardForm.CancelButton.Cancel := True" in script
    assert "CurInstallProgressChanged" in script
    assert "StageLabel.Caption := Format(CustomMessage('PhaseInstalling')" in script
    assert "StyleLabel(StageLabel, TColor(OSIA_PRIMARY_TEXT)" in script
    assert "VersionArchitecture=Version %s  ·  Windows x64" in script
    assert "PreviewState=Internal preview  ·  Unsigned build" in script
    assert "ReadySummaryLabel.Caption" in script
    assert "WizardForm.DiskSpaceLabel.Caption" in script
    assert "FinishedHeading=OSIA is ready" in script
    assert "FinishedState=SUCCESS" in script
    assert "Result := CustomMessage('ExistingServiceError')" in script
    assert "RaiseException(CustomMessage('IntegrationError'))" in script
    assert "MsgBox(CustomMessage('FirewallWarning')" in script
    assert "procedure InitializeUninstallProgressForm" in script
    # A future update-mode switch must not bypass the shared styled wizard.
    assert "ShouldSkipPage" not in script


def test_installer_branding_references_generated_assets_that_have_sources():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    build = (ROOT / "packaging" / "windows" / "build_windows.py").read_text("utf-8")
    assert (ROOT / "assets" / "osia_icon.png").is_file()
    assert (ROOT / "assets" / "osia_logo.png").is_file()
    for filename in ("osia.ico", "wizard-large.png", "wizard-small.png"):
        assert filename in script
        assert filename in build


def test_installer_identity_scope_and_output_name_are_unchanged():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "AppId={{8B8666D0-0DD4-4D1C-86D0-6B5E23A4FE36}" in script
    assert "PrivilegesRequired=lowest" in script
    assert "PrivilegesRequiredOverridesAllowed=dialog" in script
    assert "DefaultDirName={localappdata}\\Programs\\OSIA" in script
    assert "OutputBaseFilename=OSIA-Setup" in script


def _find_iscc_for_test() -> Path | None:
    candidates = (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


@pytest.mark.skipif(sys.platform != "win32", reason="Inno Setup is Windows-only")
def test_inno_script_compiles_with_a_non_installing_minimal_payload(tmp_path):
    iscc = _find_iscc_for_test()
    if iscc is None:
        pytest.skip("Inno Setup 6 is not installed")

    from PIL import Image

    source = tmp_path / "source"
    payload = source / "dist" / "OSIA"
    windows = source / "windows"
    generated = tmp_path / "build" / "generated"
    output = tmp_path / "output"
    for directory in (payload, windows, generated, output):
        directory.mkdir(parents=True)
    (payload / "OSIA.exe").write_bytes(b"non-installing compile fixture")
    (windows / "configure_lan_firewall.ps1").write_text("# fixture\n", "utf-8")
    Image.new("RGBA", (64, 64), "#c4b5f5").save(generated / "osia.ico")
    Image.new("RGB", (410, 790), "#0d0b12").save(generated / "wizard-large.png")
    Image.new("RGB", (164, 164), "#171221").save(generated / "wizard-small.png")

    completed = subprocess.run(
        [
            str(iscc),
            "/Qp",
            "/DAppVersion=1.0.0",
            f"/DRepoRoot={source}",
            f"/DBuildRoot={tmp_path / 'build'}",
            f"/DReleaseRoot={output}",
            str(ROOT / "packaging" / "windows" / "OSIA.iss"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (output / "OSIA-Setup.exe").is_file()


def test_package_spec_is_allowlist_based_and_excludes_user_config():
    spec = (ROOT / "packaging" / "windows" / "osia.spec").read_text("utf-8")
    assert 'name="OSIA"' in spec
    assert "console=False" in spec
    assert 'ROOT / "assets"' in spec
    assert 'STAGE / "models"' in spec
    assert 'STAGE / "runtimes"' in spec
    assert 'ROOT / "config"' not in spec
    assert "settings.json" not in spec
    assert "api_keys.json" not in spec
    assert '"uiautomation"' in spec


def test_build_uses_product_version_and_exact_installer_name():
    build = (ROOT / "packaging" / "windows" / "build_windows.py").read_text("utf-8")
    assert 'ROOT / "osia" / "lan_discovery.py"' in build
    assert 'installer = RELEASE / "OSIA-Setup.exe"' in build
    assert "def _write_bundle_file_manifest" in build
    assert 'CODEX_RUNTIME_VERSION = "0.144.1"' in build
    assert "def _stage_codex_runtime" in build
    assert "CODEX_RUNTIME_SHA256" in build


def test_package_smoke_requires_the_managed_codex_runtime():
    launcher = (ROOT / "packaging" / "windows" / "launcher.py").read_text("utf-8")
    assert 'resource_path("runtimes", "codex", "bin", "codex.exe")' in launcher
    assert "managed Codex runtime did not initialize" in launcher


def test_firewall_rule_is_private_local_subnet_and_executable_scoped():
    script = (ROOT / "windows" / "configure_lan_firewall.ps1").read_text("utf-8")
    for required in (
        "-Profile Private",
        "-Protocol TCP",
        "-LocalPort 8766",
        "-RemoteAddress LocalSubnet",
        "-Program ([IO.Path]::GetFullPath($targetProgram))",
    ):
        assert required in script


def test_protected_architecture_contracts_remain_intact():
    popup = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
    config = (ROOT / "osia" / "config.py").read_text("utf-8")
    server = (ROOT / "osia" / "server" / "server.py").read_text("utf-8")
    assert "POPUP_W = 440" in popup
    assert "def _resolve_config_dir" in config
    assert "from osia.config import CONFIG_DIR as _CONFIG_DIR" in server
    assert not any(
        re.search(r"^\s*MAX_RESPONSE_TOKENS\s*=", path.read_text("utf-8", errors="ignore"), re.M)
        for path in (ROOT / "osia").rglob("*.py")
    )
