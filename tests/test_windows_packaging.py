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


def test_frozen_product_version_comes_from_embedded_metadata(monkeypatch, tmp_path):
    from osia.version import current_product_version

    (tmp_path / "app_metadata.json").write_text(
        '{"version":"1.0.1"}', encoding="utf-8"
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert current_product_version() == "1.0.1"


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
        "OSIA_MAIN_BACKGROUND = $00150B11",
        "OSIA_ELEVATED_SURFACE = $001A1222",
        "OSIA_SECONDARY_SURFACE = $00251A30",
        "OSIA_ACCENT = $00F1B1D8",
        "OSIA_PRIMARY_TEXT = $00FFF9FC",
        "OSIA_SECONDARY_TEXT = $00E8D8E2",
        "OSIA_HELPER_TEXT = $00BFA9B8",
        "OSIA_DISABLED_TEXT = $008C7786",
        "OSIA_SUCCESS_TEXT = $00C8E6B2",
        "OSIA_ERROR_TEXT = $00B5B4F4",
        "OSIA_WARNING_TEXT = $0089C9F2",
    ):
        assert color in script

    # Forced dark styling keeps native dialogs readable while the background
    # remains consistently branded across the header, pages, and button rail.
    assert "WizardStyle=modern dark hidebevels includetitlebar" in script
    assert "WizardBackColor=#110b15" in script
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
    assert "WizardStyle=modern dark hidebevels includetitlebar" in script
    assert "WizardImageFileDynamicDark={#BuildRoot}\\generated\\wizard-large.png" in script
    assert "WizardSmallImageFileDynamicDark={#BuildRoot}\\generated\\wizard-small.png" in script
    assert "WizardSizePercent=112" in script
    assert "WizardForm.WizardBitmapImage.Visible := False" in script
    assert "WizardForm.WizardBitmapImage2.Visible := False" in script
    assert "WizardForm.NextButton.Default := True" in script
    assert "WizardForm.CancelButton.Cancel := True" in script
    assert "CurInstallProgressChanged" in script
    assert "StageLabel.Caption := Format(CustomMessage('PhaseInstalling')" in script
    assert "StyleLabel(StageLabel, TColor(OSIA_PRIMARY_TEXT)" in script
    assert "VersionArchitecture=Version %s  ·  Windows" in script
    assert "PreviewState=Internal preview  ·  Not signed" in script
    assert "ReadySummaryLabel.Caption" in script
    assert "WizardForm.DiskSpaceLabel.Caption" in script
    assert "FinishedHeading=OSIA is ready" in script
    assert "FinishedState=Your settings stay in your profile" in script
    assert "Result := CustomMessage('ExistingServiceError')" in script
    assert "RaiseException(CustomMessage('IntegrationError'))" in script
    assert "MsgBox(CustomMessage('FirewallWarning')" in script
    assert "procedure InitializeUninstallProgressForm" in script
    assert "function ShouldSkipPage" in script


def test_installer_branding_references_generated_assets_that_have_sources():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    build = (ROOT / "packaging" / "windows" / "build_windows.py").read_text("utf-8")
    assert (ROOT / "assets" / "osia_icon.png").is_file()
    assert (ROOT / "assets" / "osia_logo.png").is_file()
    for filename in ("osia.ico", "wizard-large.png", "wizard-small.png"):
        assert filename in script
        assert filename in build
    assert 'Image.new("RGBA", (164, 164), (0, 0, 0, 0))' in build
    assert 'transparent_logo.save(GENERATED / "wizard-small.png"' in build


def test_generated_installer_logo_has_real_transparency(monkeypatch, tmp_path):
    from PIL import Image

    path = ROOT / "packaging" / "windows" / "build_windows.py"
    spec = importlib.util.spec_from_file_location("osia_branding_build", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "GENERATED", tmp_path)

    module._generate_branding("1.0.4")
    with Image.open(tmp_path / "wizard-small.png") as image:
        alpha = image.convert("RGBA").getchannel("A")
        assert image.mode == "RGBA"
        assert alpha.getextrema() == (0, 255)
        assert all(alpha.getpixel(point) == 0 for point in (
            (0, 0), (image.width - 1, 0),
            (0, image.height - 1), (image.width - 1, image.height - 1),
        ))


def test_installer_identity_scope_and_output_name_are_unchanged():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "AppId={{8B8666D0-0DD4-4D1C-86D0-6B5E23A4FE36}" in script
    assert "PrivilegesRequired=lowest" in script
    assert "PrivilegesRequiredOverridesAllowed=commandline" in script
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
            "/DAppVersionMajor=1",
            "/DAppVersionMinor=0",
            "/DAppVersionPatch=0",
            "/DBuildChannel=internal-preview",
            "/DInternalBuild=1",
            "/DSignedBuild=0",
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
    assert "from osia.version import PRODUCT_VERSION, parse_product_version" in build
    assert "SOURCE_VERSION = PRODUCT_VERSION" in build
    assert 'PRODUCT_VERSION = "1.0.4"' in (ROOT / "osia" / "version.py").read_text("utf-8")
    assert 'installer = RELEASE / "OSIA-Setup.exe"' in build
    assert "def _write_bundle_file_manifest" in build
    assert 'CODEX_RUNTIME_VERSION = "0.144.1"' in build
    assert "def _stage_codex_runtime" in build
    assert "CODEX_RUNTIME_SHA256" in build


def test_build_identity_metadata_and_internal_override(monkeypatch, tmp_path):
    path = ROOT / "packaging" / "windows" / "build_windows.py"
    spec = importlib.util.spec_from_file_location("osia_build_windows", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "GENERATED", tmp_path)
    monkeypatch.setattr(module, "_build_commit", lambda: "a" * 40)
    monkeypatch.setattr(module, "_build_timestamp", lambda: "2026-07-20T10:00:00Z")

    baseline = module._resolve_build_identity("internal-preview", "1.0.1")
    assert baseline["version"] == "1.0.1"
    assert baseline["override"] == {
        "sourceVersion": "1.0.4", "version": "1.0.1", "channel": "internal-preview",
    }
    metadata_path = module._write_app_metadata(baseline)
    embedded = __import__("osia.updater", fromlist=["parse_app_metadata"]).parse_app_metadata(
        metadata_path.read_bytes()
    )
    assert embedded.version == "1.0.1"
    assert embedded.build_override["sourceVersion"] == "1.0.4"
    with pytest.raises(ValueError, match="forbidden"):
        module._resolve_build_identity("stable", "1.0.1")


def test_pe_inno_and_release_manifest_share_resolved_build_version():
    build = (ROOT / "packaging" / "windows" / "build_windows.py").read_text("utf-8")
    spec = (ROOT / "packaging" / "windows" / "osia.spec").read_text("utf-8")
    assert "def _generate_branding(version: str)" in build
    assert "StringStruct('ProductVersion', '{version}')" in build
    assert 'f"/DAppVersion={version}"' in build
    assert '"version": version' in build
    assert '(str(GENERATED / "app_metadata.json"), ".")' in spec


def test_wor149_update_detection_is_authoritative_and_strict():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "#error AppVersion must be supplied" in script
    assert "function HasExactUpdateSwitch" in script
    assert "CompareText(ParamStr(Index), '/OSIAUPDATE=1') = 0" in script
    assert "{param:OSIAUPDATE|0}" not in script
    assert "function InitializeSetup" in script
    assert "UpdateModeRequested := HasExactUpdateSwitch" in script
    assert "UpdateModeActive := False" in script
    assert "UpdateModeActive := True" in script
    assert "RegKeyExists(HKCU, UninstallKey)" in script
    assert "{8B8666D0-0DD4-4D1C-86D0-6B5E23A4FE36}_is1" in script
    assert "InstallLocation" in script
    assert "DisplayVersion" in script
    assert "DirExists(ExistingInstallDir)" in script
    assert "DirExists(AddBackslash(ExistingInstallDir) + '_internal')" in script
    assert "FileExists(ExistingExe)" in script
    assert "StrToVersion(RegistryVersion, RegistryPackedVersion)" in script
    assert "GetVersionComponents(ExistingExe, ExistingMajor, ExistingMinor" in script
    assert "ExistingBuild <> 0" in script
    assert "CompareStr(RegistryVersion, ExistingVersionText) <> 0" in script
    assert "SamePackedVersion(RegistryPackedVersion, ExistingPackedVersion)" in script
    assert "ComparePackedVersion(PackagePackedVersion, ExistingPackedVersion) <= 0" in script
    assert "UpdateMissingInstall" in script
    assert "UpdateInvalidDir" in script
    assert "UpdateInvalidVersion" in script
    assert "UpdateNotNewer" in script


def test_wor149_update_pages_are_dedicated_and_fresh_pages_remain():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "UpdatePage := CreateCustomPage" in script
    assert script.count("UpdatePage := CreateCustomPage") == 1
    skip_page = script[script.index("function ShouldSkipPage"):script.index("function NextButtonClick")]
    for page in (
        "wpWelcome", "wpSelectDir", "wpSelectProgramGroup", "wpSelectTasks", "wpReady",
    ):
        assert page in skip_page
    assert "UpdatePage.ID" not in skip_page
    assert "WizardForm.NextButton.Caption := CustomMessage('UpdateButton')" in script
    assert "WizardForm.CancelButton.Caption := SetupMessage(msgButtonCancel)" in script
    assert "WizardForm.Caption := CustomMessage('UpdateHeading')" in script
    assert "procedure CancelButtonClick" in script
    assert "if IsUpdateMode then\n    Confirm := False" in script
    assert "WizardForm.RunList.Visible := False" in script
    for fresh_page in (
        "WizardForm.WelcomePage", "WizardForm.SelectDirPage", "WizardForm.SelectTasksPage",
        "WizardForm.ReadyPage", "WizardForm.InstallingPage", "WizardForm.FinishedPage",
    ):
        assert fresh_page in script
    for fresh_copy in (
        "WelcomeHeading=Your OSIA, on this PC", "StepLocation=LOCATION  ·  1/3",
        "StepTasks=OPTIONS  ·  2/3", "StepReady=REVIEW  ·  3/3",
        "StepInstalling=INSTALLATION", "FinishedHeading=OSIA is ready",
    ):
        assert fresh_copy in script


def test_wor149_update_copy_progress_and_dark_controls_are_explicit():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    for copy in (
        "english.UpdateHeading=Update OSIA",
        "english.UpdateVersions=OSIA %s → OSIA %s",
        "english.UpdatePreserved=Your settings, memory, and connections will be preserved.",
        "english.UpdatePhasePreparing=Preparing the update",
        "english.UpdatePhaseClosing=Closing OSIA components",
        "english.UpdatePhaseFiles=Updating OSIA files… %d%%",
        "english.UpdatePhaseIntegration=Restoring Windows integration",
        "english.UpdatePhaseFinalizing=Finalizing",
        "english.UpdatePhaseRelaunch=Relaunching OSIA",
        "english.UpdateFinishedHeading=OSIA has been updated",
        "french.UpdateHeading=Mise à jour d’OSIA",
        "french.UpdatePhaseFiles=Mise à jour des fichiers OSIA… %d%%",
        "french.UpdateFinishedHeading=OSIA a été mise à jour",
    ):
        assert copy in script
    assert "CurInstallProgressChanged" in script
    assert "Format(CustomMessage('UpdatePhaseFiles'), [Percent])" in script
    assert "UpdateVersionsLabel.WordWrap := False" in script
    assert "UpdateVersionsLabel.Alignment := taCenter" in script
    for control, color in (
        ("UpdateVersionsLabel", "OSIA_PRIMARY_TEXT"),
        ("UpdatePreservedLabel", "OSIA_SECONDARY_TEXT"),
        ("UpdateUnsignedLabel", "OSIA_WARNING_TEXT"),
    ):
        assert f"StyleLabel({control}, TColor({color})" in script
    update_messages = "\n".join(
        line for line in script.splitlines() if ".Update" in line and "=" in line
    )
    for forbidden in (
        "Installing OSIA", "Installation d’OSIA", "Ready to install",
        "Welcome to OSIA", "first-install",
    ):
        assert forbidden not in update_messages


def test_wor149_update_is_in_place_idempotent_and_rollback_safe():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    assert "UsePreviousAppDir=yes" in script
    assert "UsePreviousTasks=yes" in script
    assert "WizardForm.DirEdit.Text := ExistingInstallDir" in script
    assert 'Type: filesandordirs; Name: "{app}\\_internal"; Check: IsUpdateMode' in script
    assert 'Name: "{app}\\*"' not in script
    assert 'Source: "{#RepoRoot}\\dist\\OSIA\\*"' in script
    assert 'Parameters: "--wake"; Flags: nowait; Check: IsUpdateMode' in script
    assert script.count('Parameters: "--wake"; Flags: nowait; Check: IsUpdateMode') == 1
    assert "WizardStyle=modern dark hidebevels includetitlebar" in script
    assert "WizardForm.ProgressGauge" in script
    assert "and (not IsUpdateMode)" in script
    assert "if IsUpdateMode then\n      RestorePreviousAutostart" in script
    assert "PrepareUpdateRollback" in script
    assert "RegisterExtraCloseApplicationsResources" in script
    assert "RegisterExtraCloseApplicationsResource(" in script
    assert "False, AddBackslash(ExistingInstallDir) + 'OSIA.exe'" in script
    assert "RenameFileWithRetry(ExistingInternal, UpdateBackupInternal)" in script
    assert "'_internal.osia-rollback'" in script
    assert "'OSIA.exe.osia-rollback'" in script
    assert "ExpandConstant('{tmp}')" not in script
    assert "ForceDirectories(BackupRoot)" not in script
    assert "DelTree(AddBackslash(ExistingInstallDir) + '_internal'" in script
    assert "RenameFileWithRetry(UpdateBackupInternal" in script
    assert "FileCopy(UpdateBackupExe, AppExe, False)" not in script
    assert "ExistingIntegrationStopped" in script
    assert "IntegrationInstalled" in script
    assert "FirewallInstalled" in script
    for forbidden in (
        "settings.json", "oauth_tokens.json", "mobile_token.txt", "memory.json",
        "task_state.json", "personality", "conversations",
    ):
        assert forbidden not in script


def test_wor149_rollback_preflight_is_local_bounded_and_diagnostic():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    prepare = script[script.index("function PrepareUpdateRollback"):script.index("function PrepareToInstall")]
    install = script[script.index("procedure CurStepChanged"):script.index("procedure InitializeUninstallProgressForm")]
    deinit = script[script.index("procedure DeinitializeSetup"):]

    for failure in (
        "invalid_source", "stale_backup", "executable_copy_failed",
        "payload_rename_failed",
    ):
        assert f"UpdateBackupFailure := '{failure}'" in prepare
    assert "for Attempt := 1 to 20" in script
    assert "for Attempt := 1 to 40" in script
    assert "Sleep(250)" in script
    assert "Log('WOR-149 rollback" in script
    assert "if not PrepareUpdateRollback then" in install
    assert "RaiseException(CustomMessage('UpdateBackupError'))" in install
    assert install.index("PrepareUpdateRollback") < install.index("UpdateDetailCopying")
    assert "AddBackslash(ExistingInstallDir) + 'OSIA.exe'" in deinit
    assert "ExpandConstant('{app}\\OSIA.exe')" in deinit
    assert "if UpdateModeRequested and (not IsUpdateMode) then" in deinit
    assert deinit.index("if IsUpdateMode then") < deinit.index("ExpandConstant('{app}\\OSIA.exe')")


def test_wor149_hotfix_redesign_is_minimal_and_uses_transparent_logo():
    script = (ROOT / "packaging" / "windows" / "OSIA.iss").read_text("utf-8")
    build = (ROOT / "packaging" / "windows" / "build_windows.py").read_text("utf-8")
    assert "WizardSizePercent=112" in script
    assert "WizardSmallImageBackColor=#110b15" in script
    assert "WizardForm.MainPanel.Color := TColor(OSIA_MAIN_BACKGROUND)" in script
    assert "WizardForm.WizardSmallBitmapImage.BackColor := TColor(OSIA_MAIN_BACKGROUND)" in script
    assert "UpdateLogo.SetBounds((UpdatePage.Surface.ClientWidth" in script
    assert "UpdatePreservedLabel.Alignment := taCenter" in script
    assert "UpdateVersionsLabel.AutoSize := False" in script
    assert "WelcomeMetaLabel.AutoSize := False" in script
    assert "WizardForm.WelcomeLabel1.Alignment := taCenter" in script
    assert "WizardForm.FinishedHeadingLabel.Alignment := taCenter" in script
    assert "WizardForm.NextButton.Font.Name := 'Segoe UI Semibold'" in script
    assert "transparent_logo = Image.new(\"RGBA\", (164, 164), (0, 0, 0, 0))" in build
    assert "mark = icon.copy()" in build


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
