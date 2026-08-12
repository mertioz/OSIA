# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 6.21 onedir build for the Windows OSIA application."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)
from osia.windows_release_policy import filter_pyinstaller_data_toc

ROOT = Path(SPECPATH).parent.parent
STAGE = ROOT / "build" / "windows"
GENERATED = STAGE / "generated"

datas = [
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "windows" / "configure_lan_firewall.ps1"), "windows"),
    (str(STAGE / "models"), "models"),
    (str(STAGE / "runtimes"), "runtimes"),
    (str(STAGE / "legal"), "legal"),
    (str(GENERATED / "app_metadata.json"), "."),
    (str(GENERATED / "personal_browser_metadata.json"), "personal_browser"),
    (str(ROOT / "packaging" / "native_messaging" / "space.osia.browser.json.in"), "personal_browser"),
    (str(ROOT / "packaging" / "windows" / "THIRD_PARTY_NOTICES.md"), "."),
]
binaries = []

hiddenimports = collect_submodules(
    "osia",
    filter=lambda name: not any(
        part.startswith("test_") or part == "_selftest"
        for part in name.split(".")
    ) and not name.startswith("osia.wake_training"),
)
for package in (
    "aiortc",
    "av",
    "faster_whisper",
    "espeakng_loader",
    "en_core_web_sm",
    "kokoro",
    "mcp",
    "playwright",
    "pypdfium2",
    "PIL",
    "reportlab",
    "rapidocr",
    "soundfile",
    "onnxruntime",
    "uiautomation",
):
    package_datas, package_bins, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_bins
    hiddenimports += package_hidden

datas += collect_data_files("misaki")
datas += collect_data_files("openwakeword")
datas += collect_data_files("language_tags")
datas += copy_metadata("en_core_web_sm")
datas += copy_metadata("reportlab")

hiddenimports += [
    "comtypes.gen",
    "comtypes.client",
    "misaki.en",
    "misaki.espeak",
    "openwakeword.model",
    "openwakeword.utils",
    "pythoncom",
    "pywintypes",
    "uiautomation",
    "win32api",
    "win32com.client",
    "win32event",
    "win32process",
]

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "jupyter",
        "matplotlib",
        "librosa",
        "llvmlite",
        "numba",
        "notebook",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)
filtered_datas, excluded_generated_data = filter_pyinstaller_data_toc(a.datas)
a.datas = filtered_datas
for destination in excluded_generated_data:
    print(f"Excluded generated package data: {destination}")
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OSIA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    icon=str(GENERATED / "osia.ico"),
    version=str(GENERATED / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OSIA",
)
