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

ROOT = Path(SPECPATH).parent.parent
STAGE = ROOT / "build" / "windows"
GENERATED = STAGE / "generated"

datas = [
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "osia" / "wake_training" / "osia_verifier.pkl"), "osia/wake_training"),
    (str(ROOT / "windows" / "configure_lan_firewall.ps1"), "windows"),
    (str(STAGE / "models"), "models"),
    (str(STAGE / "legal"), "legal"),
    (str(ROOT / "packaging" / "windows" / "THIRD_PARTY_NOTICES.md"), "."),
]
binaries = []

hiddenimports = collect_submodules(
    "osia",
    filter=lambda name: not any(
        part.startswith("test_") or part == "_selftest"
        for part in name.split(".")
    ),
)
for package in (
    "aiortc",
    "av",
    "espeakng_loader",
    "en_core_web_sm",
    "kokoro",
    "mcp",
    "playwright",
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
