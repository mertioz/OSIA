"""Keep custom-painted corners on translucent Windows widgets."""

import sys

from PySide6.QtWidgets import QApplication, QWidget


def use_painted_window_corners(window: QWidget) -> None:
    if (
        sys.platform != "win32"
        or QApplication.platformName() != "windows"
        or not window.isVisible()
    ):
        return
    hwnd = window.effectiveWinId()
    if not hwnd:
        return

    import ctypes
    from ctypes import wintypes

    # DWM rounding draws a second frame outside the painted card or capsule.
    corner_preference = 33  # DWMWA_WINDOW_CORNER_PREFERENCE
    do_not_round = ctypes.c_int(1)  # DWMWCP_DONOTROUND
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(int(hwnd)), corner_preference,
            ctypes.byref(do_not_round), ctypes.sizeof(do_not_round),
        )
    except (AttributeError, OSError):
        pass  # The corner preference is only available on newer Windows.
