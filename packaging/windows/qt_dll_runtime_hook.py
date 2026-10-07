"""Register bundled Qt DLLs before PyInstaller imports its PyQt6 runtime hook."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_QT_DLL_DIRECTORY_HANDLE = None


def register_frozen_qt_dll_directory():
    global _QT_DLL_DIRECTORY_HANDLE
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    frozen_root = str(getattr(sys, "_MEIPASS", "") or "")
    if not frozen_root:
        return None
    qt_bin = Path(frozen_root) / "PyQt6" / "Qt6" / "bin"
    if not qt_bin.is_dir():
        return None
    qt_bin_path = str(qt_bin)
    search_path = os.environ.get("PATH", "")
    if os.path.normcase(qt_bin_path) not in {
        os.path.normcase(item) for item in search_path.split(os.pathsep)
    }:
        os.environ["PATH"] = (
            qt_bin_path + (os.pathsep + search_path if search_path else "")
        )
    _QT_DLL_DIRECTORY_HANDLE = os.add_dll_directory(qt_bin_path)
    return _QT_DLL_DIRECTORY_HANDLE


register_frozen_qt_dll_directory()
