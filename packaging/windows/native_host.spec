# -*- mode: python ; coding: utf-8 -*-
"""Dedicated console onedir build for Chromium Native Messaging stdio."""

from pathlib import Path

ROOT = Path(SPECPATH).parent.parent

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "native_host_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "osia.personal_browser.protocol",
        "osia.personal_browser.registration",
        "osia.personal_browser.registration_windows",
        "osia.personal_browser.registration_posix",
        "pywintypes", "win32api", "win32con", "win32file", "win32pipe",
        "win32security",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt6", "playwright", "pytest", "tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OSIA-NativeHost",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=True,
    target_arch="x86_64",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="OSIA-NativeHost",
)
