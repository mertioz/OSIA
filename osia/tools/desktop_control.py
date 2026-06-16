#!/usr/bin/env python3
"""OS-level mouse/keyboard control for native desktop apps (WOR-42).

This is the low-level input layer behind OSIA's ``desktop_click`` /
``desktop_type`` / ``desktop_press`` / ``desktop_scroll`` agent tools. It moves
the real cursor, clicks, types, presses keys and scrolls in whatever window is
focused — so OSIA can drive NATIVE applications (file managers, settings panes,
native dialogs, games) the same way it already drives web pages through the
browser tool.

Backend per platform (resolved at call time, never hardcoded):
  * Linux Wayland  -> ``ydotool`` (needs the ``ydotoold`` daemon; ``wtype`` is a
                       typing fallback on wlroots compositors).
  * Linux X11      -> ``xdotool``.
  * Windows / Mac  -> ``pyautogui`` (declared in requirements.txt).

Every public function returns a short success string on the happy path and an
``"ERROR: ..."`` string on failure — never raises — so it plugs straight into
OSIA's anti-hallucination tool contract (a result starting with ``ERROR:`` means
the action did NOT happen). When a required backend is missing the error string
carries an actionable install hint (e.g. the ydotoold daemon on Fedora KDE
Wayland). Cross-platform: every OS-specific branch is guarded by ``sys.platform``.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import namedtuple

# ---------------------------------------------------------------------------
# Optional backend: pyautogui (Windows / Mac).
#
# Imported lazily — NOT at module import time — on purpose: on Linux we use
# ydotool/xdotool and never touch pyautogui, and importing pyautogui there would
# pull python-xlib and require a live $DISPLAY (it can fail or stall on headless
# / Wayland boxes). Loading it only when a pyautogui backend is actually selected
# keeps Linux startup clean while still degrading gracefully when it is absent
# (mirrors the graceful ``_AUDIO_OK`` sounddevice guard used across osia/voice).
# ---------------------------------------------------------------------------
_pyautogui = None
_pyautogui_err = ""
_pyautogui_loaded = False


def _load_pyautogui():
    """Import pyautogui once, caching the module (or ``None`` on failure)."""
    global _pyautogui, _pyautogui_err, _pyautogui_loaded
    if _pyautogui_loaded:
        return _pyautogui
    _pyautogui_loaded = True
    try:
        import pyautogui as _pg
        # Don't abort the whole program when the cursor lands in a screen corner.
        _pg.FAILSAFE = False
        _pyautogui = _pg
    except Exception as exc:  # missing dep / no display / no input backend
        _pyautogui = None
        _pyautogui_err = str(exc)
    return _pyautogui


# ---------------------------------------------------------------------------
# Platform / backend resolution
# ---------------------------------------------------------------------------
def _is_wayland() -> bool:
    return bool(
        os.environ.get("WAYLAND_DISPLAY")
        or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )


def _backend() -> str:
    """Return the input backend for this platform: ``pyautogui`` / ``x11`` /
    ``wayland``."""
    if sys.platform == "win32" or sys.platform == "darwin":
        return "pyautogui"
    if sys.platform.startswith("linux"):
        return "wayland" if _is_wayland() else "x11"
    return "pyautogui"  # other unixes — best effort


# ---------------------------------------------------------------------------
# Subprocess helper for the CLI backends (xdotool / ydotool / wtype)
# ---------------------------------------------------------------------------
def _run(cmd, success_msg, ctx, timeout=10, env=None):
    """Run ``cmd``; return ``success_msg`` (may be ``None``) on rc 0, else an
    ``ERROR: ...`` string. Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return f"ERROR: {ctx}: {cmd[0]} is not installed"
    except Exception as e:  # timeout, permission, etc.
        return f"ERROR: {ctx}: {e}"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip() or f"{cmd[0]} exited {r.returncode}"
        return f"ERROR: {ctx}: {msg}"
    return success_msg


# ---------------------------------------------------------------------------
# Backend readiness probes (return None when ready, else an actionable ERROR)
# ---------------------------------------------------------------------------
def _ydotool_socket_path():
    """Return an existing ydotoold socket path, or ``None``.

    ydotoold and the ydotool client don't always agree on the socket path (e.g.
    Fedora runs ``ydotoold --socket-path=/tmp/.ydotool_socket`` while the client
    defaults to ``$XDG_RUNTIME_DIR/.ydotool_socket``). Probe the known locations
    so we can point the client at the real one via ``YDOTOOL_SOCKET``.
    """
    sock = os.environ.get("YDOTOOL_SOCKET")
    if sock and os.path.exists(sock):
        return sock
    candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(os.path.join(xdg, ".ydotool_socket"))
    try:
        candidates.append(f"/run/user/{os.getuid()}/.ydotool_socket")
    except AttributeError:  # os.getuid is Unix-only; we are on Linux here anyway
        pass
    candidates.append("/tmp/.ydotool_socket")
    for c in candidates:
        try:
            if c and os.path.exists(c):
                return c
        except OSError:
            pass
    return None


def _ydotool_env():
    """Environment for ydotool *client* calls, with ``YDOTOOL_SOCKET`` pointed at
    the daemon's actual socket when the client default would otherwise miss it."""
    env = os.environ.copy()
    if not env.get("YDOTOOL_SOCKET"):
        sock = _ydotool_socket_path()
        if sock:
            env["YDOTOOL_SOCKET"] = sock
    return env


def _ydotoold_running() -> bool:
    """Best-effort detection of the ydotoold daemon: its socket, then a process
    scan (psutil is a declared dependency)."""
    if _ydotool_socket_path():
        return True
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").lower() == "ydotoold":
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _wayland_ready_error():
    if shutil.which("ydotool") is None:
        return (
            "ERROR: ydotool is not installed — required to control the mouse and "
            "keyboard on Wayland. Install it and start the daemon: "
            "sudo dnf install ydotool && sudo systemctl enable --now ydotoold"
        )
    if not _ydotoold_running():
        return (
            "ERROR: the ydotoold daemon is not running — ydotool needs it to inject "
            "input on Wayland. Start it with: sudo systemctl enable --now ydotoold "
            "(install first if missing: sudo dnf install ydotool). On Fedora KDE "
            "Wayland the daemon needs uinput access, which the systemd unit grants."
        )
    return None


def _x11_ready_error():
    if shutil.which("xdotool") is None:
        return (
            "ERROR: xdotool is not installed — required to control the mouse and "
            "keyboard on X11. Install it: sudo dnf install xdotool "
            "(Debian/Ubuntu: sudo apt install xdotool)."
        )
    return None


def _pyautogui_ready_error():
    if _load_pyautogui() is None:
        return (
            "ERROR: pyautogui is unavailable ("
            + (_pyautogui_err or "import failed")
            + ") — required for native mouse/keyboard control on this platform. "
            "Install it: pip install pyautogui"
        )
    return None


# ---------------------------------------------------------------------------
# Key maps
# ---------------------------------------------------------------------------
# ydotool's ``key`` subcommand speaks Linux input-event codes (NOT names), as
# ``code:1`` (press) / ``code:0`` (release) pairs. Map the common keys.
_YDOTOOL_KEYCODES = {
    "ctrl": 29, "control": 29, "leftctrl": 29,
    "alt": 56, "leftalt": 56, "altgr": 100, "rightalt": 100,
    "shift": 42, "leftshift": 42,
    "super": 125, "win": 125, "meta": 125, "cmd": 125, "command": 125,
    "enter": 28, "return": 28,
    "esc": 1, "escape": 1,
    "tab": 15,
    "space": 57, "spacebar": 57,
    "backspace": 14,
    "delete": 111, "del": 111,
    "insert": 110, "ins": 110,
    "home": 102, "end": 107,
    "pageup": 104, "pgup": 104, "pagedown": 109, "pgdn": 109,
    "up": 103, "down": 108, "left": 105, "right": 106,
    "capslock": 58, "minus": 12, "equal": 13,
    # letters
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34,
    "h": 35, "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49,
    "o": 24, "p": 25, "q": 16, "r": 19, "s": 31, "t": 20, "u": 22,
    "v": 47, "w": 17, "x": 45, "y": 21, "z": 44,
    # digits
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    # function keys
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
}

# xdotool ``key`` speaks XKB keysym names; map only the ones whose name differs
# from our normalized token (letters/digits/F-keys pass through).
_XDOTOOL_KEYS = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "shift": "shift",
    "super": "super", "win": "super", "meta": "super", "cmd": "super",
    "command": "super",
    "enter": "Return", "return": "Return",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete", "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pageup": "Page_Up", "pgup": "Page_Up",
    "pagedown": "Page_Down", "pgdn": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "capslock": "Caps_Lock", "minus": "minus", "equal": "equal",
}

# pyautogui key names; pass through anything already matching its vocabulary.
_PYAUTOGUI_KEYS = {
    "control": "ctrl", "ctrl": "ctrl",
    "return": "enter", "enter": "enter",
    "escape": "esc", "esc": "esc",
    "super": "win", "win": "win", "meta": "win", "cmd": "command",
    "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown",
    "spacebar": "space",
}

_PYAUTOGUI_BUTTON = {"left": "left", "right": "right", "middle": "middle"}
# ydotool ``click`` button codes: 0x40 = press, 0x80 = release, +button (0=left,
# 1=right, 2=middle). 0xC0 is a full left click (press+release).
_YDOTOOL_CLICK_CODES = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}


def _split_combo(key: str):
    return [p.strip() for p in str(key).split("+") if p.strip()]


def _xdotool_keyname(part: str) -> str:
    pl = part.lower()
    if pl in _XDOTOOL_KEYS:
        return _XDOTOOL_KEYS[pl]
    if pl.startswith("f") and pl[1:].isdigit():  # f1..f12 -> F1..F12
        return "F" + pl[1:]
    return pl  # single letters / digits pass through


def _pyautogui_keyname(part: str) -> str:
    pl = part.lower()
    return _PYAUTOGUI_KEYS.get(pl, pl)


# ---------------------------------------------------------------------------
# ydotool cursor positioning + pointer-acceleration auto-calibration
#
# On KDE Wayland ``ydotool mousemove -a`` (absolute) lands at (0,0) regardless of
# the requested target — it is effectively broken. Relative moves DO work but are
# amplified by the compositor's pointer acceleration by a factor that VARIES per
# machine / user settings / scale (≈2 on the dev box). So instead of an absolute
# move we:
#   1. reset the cursor with the (reliable) absolute-to-(0,0) corner clamp,
#   2. issue ONE relative move scaled down by an auto-calibrated factor.
# The factor is measured (the probe is repeated N times, aberrant readings
# discarded, the MEDIAN of the survivors taken — see _measure_accel_factor),
# clamped, then cached both in-process and on disk
# (CONFIG_DIR/desktop_calibration.json) so it survives restarts. Measurement
# tries two strategies in order:
#   1. xdotool cursor readback (X11/XWayland) — preferred when the cursor
#      position is live-readable;
#   2. screenshot-diff calibration (any Wayland compositor) — captures the cursor
#      with grim/spectacle before and after a probe move and locates it by image
#      diff. This is the only option on compositors with no live readback (e.g.
#      KDE Wayland, where xdotool getmouselocation is stale and kdotool 0.2.1 has
#      no cursor readback at all — only window commands).
# Set FORCE_RECALIBRATE to remeasure, or YDOTOOL_ACCEL_FACTOR to override when
# neither strategy can run. Linux-Wayland-only: reached solely from the
# ``wayland`` branch of ``click_at``.
# ---------------------------------------------------------------------------
_accel_factor = None      # auto-calibrated pointer-accel factor (None = not yet)
_screen_cache = None      # (w, h) screen size, resolved once per process

_DEFAULT_ACCEL_FACTOR = 2.0
_ACCEL_MIN, _ACCEL_MAX = 0.5, 8.0
_CALIBRATION_PROBE_PX = 300        # relative move issued during calibration
# Robust median-of-N calibration: repeat the probe, drop aberrant readings, take
# the median. Guards against a single polluted measurement (a stray on-screen
# change once skewed the screenshot-diff centroid to an absurd raw_factor of
# 0.12 vs the real ~2.0, which would have sent every click far off-target).
_CALIBRATION_SAMPLES = 3           # probe this many times per calibration
_CALIBRATION_MIN_VALID = 2         # need >= this many in-range samples, else default
_ACCEL_PLAUSIBLE_MIN = 0.8         # a single raw_factor below this is aberrant ->
_ACCEL_PLAUSIBLE_MAX = 5.0         # discarded (NOT clamped) before the median
# Screenshot-diff calibration tunables:
_DIFF_THRESHOLD = 30               # per-pixel max-channel delta counted as "changed"
_MAX_CHANGED_FRAC = 0.05           # abort if >5% of the screen changed (video/animation)
_CORNER_EXCLUDE_PX = 100           # ignore this top-left square (the cursor's OLD spot):
                                   # > a typical cursor sprite (<=64 px) yet < the nearest
                                   # the new cursor can land (probe * _ACCEL_MIN = 150 px)
# Settle delays around the probe move. The reset settle is OUTSIDE the A->B diff
# window (it stabilises the corner baseline before capture A); the probe settle
# is INSIDE it, so it is kept small to minimise the window in which something
# other than the cursor could change between captures A and B and pollute the diff.
_CALIB_RESET_SETTLE = 0.15         # after the corner reset, before capture A
_CALIB_PROBE_SETTLE = 0.06         # after the probe move, before capture B (in-window)


def _calibration_path() -> str:
    """Path to the on-disk calibration cache, under the resolved CONFIG_DIR.

    CONFIG_DIR is imported from ``osia.config`` (never hardcoded) so the cache
    lands in the SAME dir the rest of OSIA uses on every OS (CLAUDE.md)."""
    from osia.config import CONFIG_DIR  # local import: a constant, no coupling
    return os.path.join(CONFIG_DIR, "desktop_calibration.json")


def _load_cached_factor():
    """Return the persisted, in-range acceleration factor, or None."""
    try:
        with open(_calibration_path(), "r", encoding="utf-8") as fh:
            f = float(json.load(fh).get("accel_factor"))
        if _ACCEL_MIN <= f <= _ACCEL_MAX:
            return f
    except Exception:
        pass
    return None


def _save_cached_factor(factor, **meta) -> None:
    """Persist the calibrated factor under CONFIG_DIR (best-effort, never raises)."""
    try:
        path = _calibration_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rec = {"accel_factor": round(float(factor), 4)}
        rec.update(meta)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2)
    except Exception:
        pass


def _parse_mouselocation(text: str):
    """Parse ``x:N y:N ...`` (xdotool / kdotool getmouselocation) -> (x, y)."""
    mx = re.search(r"x:(-?\d+)", text or "")
    my = re.search(r"y:(-?\d+)", text or "")
    if mx and my:
        return int(mx.group(1)), int(my.group(1))
    return None


def _xdotool_readback(env):
    """Live cursor position as (x, y) via xdotool — X11/XWayland only. Returns
    None when xdotool is missing, there is no DISPLAY, or the read fails. (On KDE
    Wayland ``xdotool getmouselocation`` is stale, so this yields a usable value
    only where the X server actually tracks the pointer.)"""
    if not (shutil.which("xdotool") and (env or os.environ).get("DISPLAY")):
        return None
    try:
        r = subprocess.run(["xdotool", "getmouselocation"],
                           capture_output=True, text=True, timeout=3, env=env)
        if r.returncode == 0:
            return _parse_mouselocation(r.stdout)
    except Exception:
        pass
    return None


def _measure_via_xdotool_readback(env):
    """X11/XWayland strategy: measure ydotool's relative-move amplification by
    reading the real cursor position before and after a known probe move ->
    factor = travelled / probe. Returns the factor, or None when xdotool readback
    is unavailable or no movement was observed (the cursor is NOT moved in that
    case — the caller then tries the screenshot strategy)."""
    if _xdotool_readback(env) is None:       # no live readback -> let caller try screenshots
        return None
    if _run(["ydotool", "mousemove", "-a", "-x", "0", "-y", "0"],
            None, "calibrate", env=env) is not None:
        return None
    time.sleep(0.12)
    p0 = _xdotool_readback(env)
    if p0 is None:
        return None
    if _run(["ydotool", "mousemove", "-x", str(_CALIBRATION_PROBE_PX),
             "-y", str(_CALIBRATION_PROBE_PX)], None, "calibrate", env=env) is not None:
        return None
    time.sleep(0.12)
    p1 = _xdotool_readback(env)
    if p1 is None:
        return None
    deltas = [p1[0] - p0[0], p1[1] - p0[1]]
    factors = [d / _CALIBRATION_PROBE_PX for d in deltas if d > 0]
    if not factors:
        return None
    return sum(factors) / len(factors)


def _screenshot_with_cursor(path, env) -> bool:
    """Capture the full screen INCLUDING the cursor to ``path``; return True on
    success. Uses ``spectacle -b -n -p -o`` (the ``-p`` includes the pointer; KDE)
    or ``grim -c`` (the ``-c`` includes cursors; wlroots) — whichever binary
    exists, spectacle first to match ``agent._capture_screen``'s order (on KDE,
    grim may produce a cursor-less frame). Mirrors that chain's robustness: drop
    any stale file first, then require rc 0 AND a non-empty file."""
    backends = []
    if shutil.which("spectacle"):
        backends.append((["spectacle", "-b", "-n", "-p", "-o", path], 15))  # KDE (-p pointer)
    if shutil.which("grim"):
        backends.append((["grim", "-c", path], 10))                         # wlroots (-c cursor)
    for cmd, tmo in backends:
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=tmo, env=env)
        except Exception:
            continue
        if r.returncode == 0:
            try:
                if os.path.getsize(path) > 0:
                    return True
            except OSError:
                pass
    return False


def _measure_via_screenshot_diff(env):
    """Any-Wayland-compositor strategy: locate the cursor by image diff.

    reset to (0,0) -> capture A -> relative probe move -> capture B. The absolute
    diff of A and B has two blobs: the cursor gone from the top-left corner and
    the cursor arrived at ``probe * factor``. The centroid of the changed region
    that is NOT in the top-left corner is the cursor's new position, so
    ``factor = mean(centroid_x / probe, centroid_y / probe)`` (the per-axis
    factors averaged).

    Returns the factor, or None when it cannot run or a guard trips: no
    cursor-capable screenshot tool, PIL/numpy unavailable, an empty diff (cursor
    never moved / not captured), or >5% of the screen changed (something else
    moved, e.g. a playing video) — every None routes the caller to the env/default
    factor with a warning."""
    if not (shutil.which("spectacle") or shutil.which("grim")):
        return None                          # no cursor-capable screenshot tool
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None                          # PIL/numpy unavailable -> caller defaults
    tmpdir = tempfile.gettempdir()
    pid = os.getpid()
    path_a = os.path.join(tmpdir, f"osia_calib_a_{pid}.png")
    path_b = os.path.join(tmpdir, f"osia_calib_b_{pid}.png")
    try:
        # A: cursor reset into the top-left corner. Settle the corner baseline
        # FIRST (this delay is outside the A->B window), then capture A as the
        # last thing before the probe move so nothing widens the diff window.
        if _run(["ydotool", "mousemove", "-a", "-x", "0", "-y", "0"],
                None, "calibrate", env=env) is not None:
            return None
        time.sleep(_CALIB_RESET_SETTLE)
        if not _screenshot_with_cursor(path_a, env):
            return None
        # B: probe move (cursor lands at probe * factor) then capture B as soon as
        # it has repainted. Capture A -> probe -> capture B are kept back-to-back
        # with only the minimal _CALIB_PROBE_SETTLE between them, so the window in
        # which an unrelated on-screen change could pollute the diff is as short
        # as possible.
        if _run(["ydotool", "mousemove", "-x", str(_CALIBRATION_PROBE_PX),
                 "-y", str(_CALIBRATION_PROBE_PX)], None, "calibrate", env=env) is not None:
            return None
        time.sleep(_CALIB_PROBE_SETTLE)
        if not _screenshot_with_cursor(path_b, env):
            return None
        a = np.asarray(Image.open(path_a).convert("RGB"), dtype=np.int16)
        b = np.asarray(Image.open(path_b).convert("RGB"), dtype=np.int16)
        if a.shape != b.shape or a.ndim != 3:
            return None
        changed = np.abs(b - a).max(axis=2) > _DIFF_THRESHOLD   # 2D bool mask (y, x)
        h, w = changed.shape
        n_changed = int(changed.sum())
        # Guard: nothing moved, or too much moved (video/animation, not the cursor).
        if n_changed == 0 or n_changed > h * w * _MAX_CHANGED_FRAC:
            return None
        # Drop the top-left square (the cursor's OLD position) -> keep the new blob.
        changed[:_CORNER_EXCLUDE_PX, :_CORNER_EXCLUDE_PX] = False
        ys, xs = np.nonzero(changed)
        if xs.size == 0:                     # only the corner changed -> inconclusive
            return None
        fx = float(xs.mean()) / _CALIBRATION_PROBE_PX
        fy = float(ys.mean()) / _CALIBRATION_PROBE_PX
        factor = (fx + fy) / 2.0
        return factor if factor > 0 else None
    except Exception:
        return None
    finally:
        for p in (path_a, path_b):
            try:
                os.remove(p)
            except OSError:
                pass


def _measure_accel_factor_once(env):
    """One measurement attempt at ydotool's relative-move amplification factor,
    trying the xdotool-readback strategy (X11/XWayland, preferred) then the
    screenshot-diff strategy (any Wayland compositor). Returns the raw measured
    factor, or None when neither can run / no movement was observed.

    kdotool 0.2.1 has no cursor readback (only window commands) and the
    KWin-D-Bus readback was unreliable, so both old readback paths were removed
    in favour of the screenshot diff, which works on any Wayland compositor."""
    f = _measure_via_xdotool_readback(env)
    if f is not None:
        return f
    return _measure_via_screenshot_diff(env)


def _measure_accel_factor(env):
    """Robustly measure the amplification factor: repeat the single-shot probe
    ``_CALIBRATION_SAMPLES`` times, DISCARD aberrant readings (raw_factor outside
    ``[_ACCEL_PLAUSIBLE_MIN, _ACCEL_PLAUSIBLE_MAX]``) rather than clamping them,
    and return the MEDIAN of the survivors. Returns None when fewer than
    ``_CALIBRATION_MIN_VALID`` valid samples remain — the caller then falls back
    to the YDOTOOL_ACCEL_FACTOR env / default factor with a warning.

    A single screenshot-diff measurement can be polluted when something other
    than the cursor changes between captures A and B (a stray repaint once gave
    an absurd raw_factor of 0.12 vs the real ~2.0). Taking the median of several
    in-range probes rejects such an outlier instead of caching a wrong factor
    that would send every click far off-target."""
    valid = []
    for _ in range(_CALIBRATION_SAMPLES):
        f = _measure_accel_factor_once(env)
        if f is None:
            continue                       # strategy could not run -> just remeasure
        if _ACCEL_PLAUSIBLE_MIN <= f <= _ACCEL_PLAUSIBLE_MAX:
            valid.append(f)
        # else: aberrant reading -> discard (do NOT clamp); the loop remeasures.
    if len(valid) < _CALIBRATION_MIN_VALID:
        return None
    return statistics.median(valid)


def _calibrate_accel_factor(env):
    """Return the pointer-acceleration factor for relative ydotool moves.

    Resolved once per process (memoized) and lazily — on the first click. Order:
    in-process cache -> on-disk cache (unless FORCE_RECALIBRATE) -> live
    measurement (_measure_accel_factor) -> YDOTOOL_ACCEL_FACTOR env / 2.0 default
    (with a clear warning). Only a real measurement is persisted, so installing a
    screenshot tool (grim/spectacle) later upgrades a defaulted box to a real
    calibration on its next run."""
    global _accel_factor
    if _accel_factor is not None:
        return _accel_factor
    if not os.environ.get("FORCE_RECALIBRATE"):
        cached = _load_cached_factor()
        if cached is not None:
            _accel_factor = cached
            return cached
    measured = _measure_accel_factor(env)
    if measured is not None:
        factor = max(_ACCEL_MIN, min(_ACCEL_MAX, measured))
        _accel_factor = factor
        _save_cached_factor(factor, method="measured-median",
                            raw_factor=round(measured, 4), probe_px=_CALIBRATION_PROBE_PX,
                            samples=_CALIBRATION_SAMPLES)
        return factor
    raw = os.environ.get("YDOTOOL_ACCEL_FACTOR", "").strip()
    try:
        factor = float(raw) if raw else _DEFAULT_ACCEL_FACTOR
    except ValueError:
        factor = _DEFAULT_ACCEL_FACTOR
    factor = max(_ACCEL_MIN, min(_ACCEL_MAX, factor))
    _accel_factor = factor
    print(
        "[desktop_control] WARNING: could not auto-calibrate mouse pointer "
        "acceleration (screenshot-diff calibration unavailable or inconclusive); "
        f"using factor {factor}. Install grim (wlroots) or spectacle (KDE) plus "
        "Pillow for screenshot calibration, or set YDOTOOL_ACCEL_FACTOR to "
        "override.",
        file=sys.stderr,
    )
    return factor   # deliberately NOT persisted (see docstring)


def _screen_size():
    """Screen size (w, h) for clamping click targets. Tries kscreen-doctor (KDE
    Wayland) then xrandr (X11/XWayland); falls back to 1920x1080. Uses the
    physical mode resolution (never undersized vs the screenshot space, so the
    clamp can't chop a valid target). Cached once per process."""
    global _screen_cache
    if _screen_cache is not None:
        return _screen_cache
    w = h = None
    if shutil.which("kscreen-doctor"):
        try:
            r = subprocess.run(["kscreen-doctor", "-o"],
                               capture_output=True, text=True, timeout=4)
            if r.returncode == 0:
                clean = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)  # strip ANSI colour
                m = re.search(r"(\d+)x(\d+)@[0-9.]+\*", clean)   # the active (*) mode
                if m:
                    w, h = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    if (w is None or h is None) and shutil.which("xrandr"):
        try:
            r = subprocess.run(["xrandr"], capture_output=True, text=True, timeout=4)
            m = re.search(r"current\s+(\d+)\s*x\s*(\d+)", r.stdout or "")
            if m:
                w, h = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    if not w or not h:
        w, h = 1920, 1080
    _screen_cache = (w, h)
    return _screen_cache


def _ydotool_position(x, y, env):
    """Position the cursor at absolute screen pixel ``(x, y)`` on Wayland, working
    around ydotool's broken absolute move. Returns None on success or an
    ``ERROR: ...`` string on failure (matching the ``_run`` contract).

    ``mousemove -a`` lands at (0,0) on KDE Wayland regardless of target, so we use
    that as a reliable corner reset, then issue ONE relative move scaled down by
    the auto-calibrated pointer-acceleration factor."""
    factor = _calibrate_accel_factor(env) or _DEFAULT_ACCEL_FACTOR
    w, h = _screen_size()
    tx = max(0, min(int(round(x)), w - 1))
    ty = max(0, min(int(round(y)), h - 1))
    reset = _run(["ydotool", "mousemove", "-a", "-x", "0", "-y", "0"],
                 None, "click_at", env=env)
    if reset is not None:
        return reset
    time.sleep(0.1)
    rx = int(round(tx / factor))
    ry = int(round(ty / factor))
    return _run(["ydotool", "mousemove", "-x", str(rx), "-y", str(ry)],
                None, "click_at", env=env)


# ---------------------------------------------------------------------------
# Public input primitives
# ---------------------------------------------------------------------------
def click_at(x, y, button: str = "left", double: bool = False):
    """Move the cursor to absolute screen pixel ``(x, y)`` and click."""
    try:
        x = int(round(float(x)))
        y = int(round(float(y)))
    except (TypeError, ValueError):
        return f"ERROR: click_at: x and y must be numbers, got {x!r}, {y!r}"
    button = (button or "left").lower()
    if button not in ("left", "right", "middle"):
        return f"ERROR: click_at: button must be left/right/middle, got {button!r}"
    ok = f"clicked {button}{' (double)' if double else ''} at ({x}, {y})"
    backend = _backend()

    if backend == "pyautogui":
        err = _pyautogui_ready_error()
        if err:
            return err
        pg = _load_pyautogui()
        try:
            btn = _PYAUTOGUI_BUTTON.get(button, "left")
            if double:
                pg.doubleClick(x, y, button=btn)
            else:
                pg.click(x, y, button=btn)
            return ok
        except Exception as e:
            return f"ERROR: click_at via pyautogui: {e}"

    if backend == "x11":
        err = _x11_ready_error()
        if err:
            return err
        btn = {"left": "1", "middle": "2", "right": "3"}[button]
        cmd = ["xdotool", "mousemove", str(x), str(y), "click"]
        if double:
            cmd += ["--repeat", "2"]
        cmd.append(btn)
        return _run(cmd, ok, "click_at")

    # wayland
    err = _wayland_ready_error()
    if err:
        return err
    env = _ydotool_env()
    moved = _ydotool_position(x, y, env)  # calibrated reset-to-corner + relative move
    if moved is not None:
        return moved
    time.sleep(0.05)  # let the compositor register the move before clicking
    cmd = ["ydotool", "click"]
    if double:
        cmd += ["--repeat", "2"]
    cmd.append(_YDOTOOL_CLICK_CODES[button])
    return _run(cmd, ok, "click_at", env=env)


def move_to(x, y):
    """Move the cursor to absolute screen pixel ``(x, y)`` WITHOUT clicking.

    Used by the two-pass ``desktop_click`` refinement (agent.py): the coarse
    locate moves the cursor here so a cropped screenshot can be taken around the
    target, then the fine locate clicks the corrected position. Same backend
    resolution and calibrated Wayland positioning as :func:`click_at`; returns a
    short success string or an ``ERROR: ...`` string (never raises)."""
    try:
        x = int(round(float(x)))
        y = int(round(float(y)))
    except (TypeError, ValueError):
        return f"ERROR: move_to: x and y must be numbers, got {x!r}, {y!r}"
    ok = f"moved to ({x}, {y})"
    backend = _backend()

    if backend == "pyautogui":
        err = _pyautogui_ready_error()
        if err:
            return err
        pg = _load_pyautogui()
        try:
            pg.moveTo(x, y)
            return ok
        except Exception as e:
            return f"ERROR: move_to via pyautogui: {e}"

    if backend == "x11":
        err = _x11_ready_error()
        if err:
            return err
        return _run(["xdotool", "mousemove", str(x), str(y)], ok, "move_to")

    # wayland
    err = _wayland_ready_error()
    if err:
        return err
    env = _ydotool_env()
    moved = _ydotool_position(x, y, env)  # calibrated reset-to-corner + relative move
    if moved is not None:
        return moved
    return ok


def type_text(text):
    """Type ``text`` into the currently focused element."""
    if text is None:
        return "ERROR: type_text: no text provided"
    text = str(text)
    if text == "":
        return "type_text: empty string — nothing to type"
    ok = f"typed {len(text)} characters"
    backend = _backend()

    if backend == "pyautogui":
        err = _pyautogui_ready_error()
        if err:
            return err
        pg = _load_pyautogui()
        try:
            pg.write(text)
            return ok
        except Exception as e:
            return f"ERROR: type_text via pyautogui: {e}"

    if backend == "x11":
        err = _x11_ready_error()
        if err:
            return err
        return _run(["xdotool", "type", "--", text], ok, "type_text")

    # wayland: prefer ydotool; fall back to wtype (wlroots) for typing per WOR-42.
    # ``--`` ends option parsing (both use getopt_long) so text starting with
    # '-' is typed literally instead of being read as a flag.
    err = _wayland_ready_error()
    if err:
        if shutil.which("wtype"):
            alt = _run(["wtype", "--", text], f"{ok} (via wtype)", "type_text")
            if not (isinstance(alt, str) and alt.startswith("ERROR")):
                return alt
        return err
    res = _run(["ydotool", "type", "--", text], ok, "type_text", env=_ydotool_env())
    if isinstance(res, str) and res.startswith("ERROR") and shutil.which("wtype"):
        alt = _run(["wtype", "--", text], f"{ok} (via wtype)", "type_text")
        if not (isinstance(alt, str) and alt.startswith("ERROR")):
            return alt
    return res


def press_key(key):
    """Press a single key or a ``+``-joined combo (e.g. ``"enter"``, ``"ctrl+s"``,
    ``"alt+F4"``) on the focused window."""
    if key is None or not str(key).strip():
        return "ERROR: press_key: no key provided"
    parts = _split_combo(key)
    if not parts:
        return "ERROR: press_key: no key provided"
    ok = f"pressed {str(key).strip()}"
    backend = _backend()

    if backend == "pyautogui":
        err = _pyautogui_ready_error()
        if err:
            return err
        pg = _load_pyautogui()
        names = [_pyautogui_keyname(p) for p in parts]
        try:
            if len(names) == 1:
                pg.press(names[0])
            else:
                pg.hotkey(*names)
            return ok
        except Exception as e:
            return f"ERROR: press_key via pyautogui: {e}"

    if backend == "x11":
        err = _x11_ready_error()
        if err:
            return err
        keyspec = "+".join(_xdotool_keyname(p) for p in parts)
        return _run(["xdotool", "key", keyspec], ok, "press_key")

    # wayland (ydotool keycodes)
    err = _wayland_ready_error()
    if err:
        return err
    codes = []
    for p in parts:
        code = _YDOTOOL_KEYCODES.get(p.lower())
        if code is None:
            return (
                f"ERROR: press_key: key '{p}' is not supported on Wayland (ydotool). "
                "Supported: letters, digits, F1-F12, and enter, tab, esc, space, "
                "backspace, delete, ctrl, alt, shift, super, the arrows, "
                "home/end and pageup/pagedown."
            )
        codes.append(code)
    seq = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
    return _run(["ydotool", "key"] + seq, ok, "press_key", env=_ydotool_env())


def scroll(direction: str = "down", amount=3):
    """Scroll the active window ``up``/``down``/``left``/``right`` by ``amount``
    wheel notches (default 3)."""
    d = str(direction or "down").strip().lower()
    if d not in ("up", "down", "left", "right"):
        return f"ERROR: scroll: direction must be up/down/left/right, got {direction!r}"
    try:
        n = int(amount)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 50))
    backend = _backend()

    if backend == "pyautogui":
        err = _pyautogui_ready_error()
        if err:
            return err
        pg = _load_pyautogui()
        try:
            clicks = n * 100
            if d in ("up", "down"):
                pg.scroll(clicks if d == "up" else -clicks)
            else:
                pg.hscroll(clicks if d == "right" else -clicks)
            return f"scrolled {d} ({n})"
        except Exception as e:
            return f"ERROR: scroll via pyautogui: {e}"

    if backend == "x11":
        err = _x11_ready_error()
        if err:
            return err
        btn = {"up": "4", "down": "5", "left": "6", "right": "7"}[d]
        return _run(["xdotool", "click", "--repeat", str(n), btn], f"scrolled {d} ({n})", "scroll")

    # wayland: ydotool has no mouse-wheel command, so approximate with arrow keys
    err = _wayland_ready_error()
    if err:
        return err
    code = _YDOTOOL_KEYCODES[d]  # up/down/left/right are in the keycode map
    seq = []
    for _ in range(n * 3):
        seq += [f"{code}:1", f"{code}:0"]
    res = _run(["ydotool", "key"] + seq, None, "scroll", env=_ydotool_env())
    if isinstance(res, str) and res.startswith("ERROR"):
        return res
    return (
        f"scrolled {d} ({n}) using {d}-arrow keypresses "
        "(Wayland/ydotool has no mouse-wheel command — approximated with the keyboard)"
    )


# ---------------------------------------------------------------------------
# Foreground-window readiness (WOR-50 follow-up): after open_application launches
# an app, confirm the app's window has actually APPEARED and taken foreground
# focus before any desktop_type / desktop_click runs — otherwise keystrokes fire
# into the void or the wrong window while a slow app (IDE, Photoshop) is still
# opening. This is ACTIVE WAITING (poll, no fixed sleep): we return the instant a
# new foreground window stabilises (so fast apps like notepad are not slowed) and
# ERROR only if nothing appears within a generous timeout.
#
# Foreground detection is per-platform and degrades gracefully where no portable
# API is wired (the caller then reports the launch WITHOUT the readiness guarantee
# rather than emitting a false error):
#   * Windows  -> win32gui.GetForegroundWindow (+ GetWindowText / GetClassName /
#                 GetWindowThreadProcessId) via pywin32 (a declared Windows-only
#                 dependency). Console-host windows (the transient ``cmd /c <app>``
#                 shell that subprocess.Popen(shell=True) spawns) are skipped by
#                 class so we lock onto the real app window, never the launch shell.
#   * Linux X11 -> ``xdotool getactivewindow`` (already required for X11 input).
#   * Linux Wayland -> ``kdotool getactivewindow`` when present (best effort; it
#                 exposes no pid, so OSIA's own window cannot be filtered there)
#                 else unavailable.
#   * macOS / other -> unavailable (no pyobjc dependency) -> graceful fallback.
# Every branch is guarded by ``sys.platform`` (cross-platform rule).
# ---------------------------------------------------------------------------
APP_WINDOW_TIMEOUT = 14.0          # generous cap: heavy apps can take >10s to show
# WOR-80: open_application uses a LONGER launch wait — cold/heavy apps (Steam,
# games, IDEs) can take well over 14s to draw their first window, which used to
# time out into a FALSE ERROR. The caller clamps the effective deadline to stay
# inside the 30s tool-execution budget, and never ERRORs while the app's process
# is actually running (see agent.tool_open_application).
APP_LAUNCH_WINDOW_TIMEOUT = 25.0
_APP_WINDOW_POLL = 0.2             # poll interval (~200ms)
_APP_WINDOW_STABLE_POLLS = 2       # consecutive polls a new fg window must persist

# conhost / terminal host window classes — the transient ``cmd /c <app>`` shell
# that subprocess.Popen(shell=True) spawns on Windows. Skipped so the readiness
# wait locks onto the real app window, never the launch shell.
_WIN_CONSOLE_CLASSES = {
    "ConsoleWindowClass",            # classic conhost console
    "CASCADIA_HOSTING_WINDOW_CLASS", # Windows Terminal
    "PseudoConsoleWindow",
}

_win32 = None
_win32_loaded = False


def _load_win32():
    """Import pywin32's ``win32gui``/``win32process`` once (Windows only), caching
    the pair or ``None``. Mirrors the lazy, cached :func:`_load_pyautogui` guard so
    a missing/broken pywin32 degrades gracefully instead of raising."""
    global _win32, _win32_loaded
    if _win32_loaded:
        return _win32
    _win32_loaded = True
    if sys.platform != "win32":
        _win32 = None
        return None
    try:
        import win32gui
        import win32process
        _win32 = (win32gui, win32process)
    except Exception:
        _win32 = None
    return _win32


def _foreground_available() -> bool:
    """True when this platform has a foreground-window API wired (so the readiness
    wait can run); False -> the caller degrades gracefully (no wait, no false
    error)."""
    if sys.platform == "win32":
        return _load_win32() is not None
    if sys.platform.startswith("linux"):
        if _is_wayland():
            return shutil.which("kdotool") is not None
        return shutil.which("xdotool") is not None
    return False


def _foreground_window_win32():
    """(hwnd, title, pid) of the foreground window on Windows, or ``(0, "", 0)``
    when nothing usable is focused (invisible window, or a console-host shell we
    deliberately skip). ``None`` only if pywin32 is unavailable."""
    w = _load_win32()
    if not w:
        return None
    win32gui, win32process = w
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or not win32gui.IsWindowVisible(hwnd):
            return (0, "", 0)
        try:
            cls = win32gui.GetClassName(hwnd) or ""
        except Exception:
            cls = ""
        if cls in _WIN_CONSOLE_CLASSES:        # the launch shell, not the app
            return (0, "", 0)
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pid = 0
        return (int(hwnd), title, int(pid or 0))
    except Exception:
        return (0, "", 0)


def _foreground_window_xdotool():
    """(window-id, title, pid) of the active window on X11 via xdotool, or
    ``(0, "", 0)`` when there is no active window. ``None`` if xdotool is missing."""
    if shutil.which("xdotool") is None:
        return None
    try:
        r = subprocess.run(["xdotool", "getactivewindow"],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return (0, "", 0)
    wid = (r.stdout or "").strip()
    if r.returncode != 0 or not wid.isdigit():
        return (0, "", 0)                      # no active window right now
    title, pid = "", 0
    try:
        rt = subprocess.run(["xdotool", "getwindowname", wid],
                           capture_output=True, text=True, timeout=3)
        if rt.returncode == 0:
            title = (rt.stdout or "").strip()
    except Exception:
        pass
    try:
        rp = subprocess.run(["xdotool", "getwindowpid", wid],
                           capture_output=True, text=True, timeout=3)
        out = (rp.stdout or "").strip()
        if rp.returncode == 0 and out.isdigit():
            pid = int(out)
    except Exception:
        pass
    return (int(wid), title, pid)


def _foreground_window_kdotool():
    """(window-uuid, title, 0) of the active window on KDE Wayland via kdotool, or
    ``(0, "", 0)`` when none. ``None`` if kdotool is missing. Best effort: kdotool
    exposes no pid, so OSIA's own window cannot be filtered on Wayland."""
    if shutil.which("kdotool") is None:
        return None
    try:
        r = subprocess.run(["kdotool", "getactivewindow"],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return (0, "", 0)
    wid = (r.stdout or "").strip()
    if r.returncode != 0 or not wid:
        return (0, "", 0)
    title = ""
    try:
        rt = subprocess.run(["kdotool", "getwindowname", wid],
                           capture_output=True, text=True, timeout=3)
        if rt.returncode == 0:
            title = (rt.stdout or "").strip()
    except Exception:
        pass
    return (wid, title, 0)


def _foreground_window():
    """(id, title, pid) of the current foreground window, or ``None`` when no
    foreground API is wired on this platform. ``id`` is an opaque per-process
    identity used only for change detection (int hwnd / X11 id on Win/X11, str
    UUID on Wayland); ``title``/``pid`` may be ``""``/``0`` when nothing usable is
    focused. Never raises."""
    if sys.platform == "win32":
        return _foreground_window_win32()
    if sys.platform.startswith("linux"):
        return _foreground_window_kdotool() if _is_wayland() else _foreground_window_xdotool()
    return None


def foreground_snapshot():
    """Capture the current foreground-window identity BEFORE launching an app, so
    :func:`wait_for_app_window` can detect when focus has moved to the NEW window.
    Returns the ``(id, title, pid)`` tuple, or ``None`` when detection is
    unavailable on this platform. Never raises."""
    if not _foreground_available():
        return None
    return _foreground_window()


def wait_for_app_window(before, timeout=APP_WINDOW_TIMEOUT,
                        poll=_APP_WINDOW_POLL, stable_polls=_APP_WINDOW_STABLE_POLLS):
    """Active-wait until a NEW foreground window appears and stabilises after an app
    launch, so a following desktop_type/desktop_click cannot fire into the void or
    the wrong window.

    ``before`` is the pre-launch identity from :func:`foreground_snapshot`. Polls
    every ``poll`` s up to ``timeout`` s and returns a ``(status, detail)`` tuple:
      * ``("ready", <window title>)`` — a visible, titled window that is NOT the
        pre-launch foreground and NOT OSIA's own window has held foreground for
        ``stable_polls`` consecutive polls. Returns the INSTANT this happens, so a
        fast app (notepad) adds only a couple hundred ms, while a slow one is
        waited for up to ``timeout``.
      * ``("timeout", "")`` — detection works here but no such window appeared
        within ``timeout`` (the app did not open a window) -> the caller ERRORs.
      * ``("unavailable", "")`` — no foreground API on this platform -> the caller
        degrades gracefully (reports the launch without the readiness guarantee).
    Never raises.
    """
    if not _foreground_available():
        return ("unavailable", "")
    own = os.getpid()
    before_id = before[0] if before else None
    stable_id, stable_count = None, 0
    deadline = time.monotonic() + max(1.0, float(timeout))
    while True:
        fg = _foreground_window()
        if fg is None:                         # API vanished mid-wait -> degrade
            return ("unavailable", "")
        fid, ftitle, fpid = fg
        # A genuine app window: a real, titled, visible window that is neither the
        # window focused before the launch nor one of OSIA's own windows.
        is_new_app = bool(fid) and fid != before_id and fpid != own and bool(ftitle.strip())
        if is_new_app:
            if fid == stable_id:
                stable_count += 1
            else:
                stable_id, stable_count = fid, 1
            if stable_count >= stable_polls:
                return ("ready", ftitle.strip())
        else:
            stable_id, stable_count = None, 0
        if time.monotonic() >= deadline:
            return ("timeout", "")
        time.sleep(poll)


# ---------------------------------------------------------------------------
# Already-open detection + focus (WOR-50 follow-up #2): before launching, check
# whether the requested app ALREADY has an open window and, if so, bring that
# window to the foreground instead of launching a second instance. This also
# removes the documented false-ERROR edge case where an already-open+focused app
# would otherwise time out in wait_for_app_window (no NEW window ever appears).
#
# Matching is by PROCESS NAME, not window title: titles rarely contain the app
# name ("Untitled - Notepad", "… - Google Chrome"), but the owning process does
# (notepad.exe, chrome.exe). We enumerate visible top-level app windows
# (EnumWindows, Z-ordered), resolve each window's pid -> process name (psutil, a
# declared dependency), and require an EXACT executable-base match ('notepad' ==
# 'notepad') so we never focus the wrong app. Among several windows of one app we
# take the topmost (most-recently-active). If nothing matches confidently we
# return None and the caller launches as before — a wrong-window focus is worse
# than a relaunch.
#
# Windows-only (pywin32 + psutil). On Linux/Mac reliable name->process->window
# mapping is not wired here, so find_app_window returns None and the caller falls
# back to the existing launch-and-wait path (no regression, no false error).
# ---------------------------------------------------------------------------
APP_FOCUS_TIMEOUT = 4.0   # focusing an already-open window should be near-instant

# Desktop shell / taskbar / start-menu window classes — never "the app" even when
# their owning process (explorer.exe) matches; skipped so "open explorer" focuses
# a File Explorer window, never the desktop or taskbar.
_WIN_SHELL_CLASSES = {
    "Progman", "WorkerW",                        # desktop
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd",   # taskbars
    "DV2ControlHost",                            # start menu (legacy)
    "Windows.UI.Core.CoreWindow",                # start / search (modern)
}

_EXE_EXTS = (".exe", ".com", ".bat", ".cmd", ".lnk", ".app")


def _norm_app_token(name) -> str:
    """Reduce a launch string ('notepad', 'notepad.exe doc.txt', a quoted path) to
    its lowercase executable base ('notepad') for process-name matching, or '' when
    it can't be reduced. Best-effort: a wrong reduction just means no match -> the
    caller launches (safe)."""
    s = (name or "").strip()
    if not s:
        return ""
    if s[0] in "\"'":                             # leading quoted path with spaces
        end = s.find(s[0], 1)
        first = s[1:end] if end > 1 else s[1:]
    else:
        first = s.split()[0]
    base = os.path.basename(first.replace("\\", "/"))
    root, ext = os.path.splitext(base)
    if ext.lower() in _EXE_EXTS:
        base = root
    return base.strip().lower()


def _proc_base(proc_name) -> str:
    """'notepad.exe' -> 'notepad' (lowercase); strips only known executable exts."""
    root, ext = os.path.splitext(proc_name or "")
    if ext.lower() in _EXE_EXTS:
        return root.lower()
    return (proc_name or "").strip().lower()


def _enum_app_windows_win32():
    """Visible, titled, top-level application windows as (hwnd, title, pid, cls) in
    Z-order (topmost first). Excludes OSIA's own windows, owned/child windows, tool
    windows, console hosts and the desktop shell. Windows-only; [] if pywin32 is
    missing. Never raises."""
    w = _load_win32()
    if not w:
        return []
    win32gui, win32process = w
    try:
        import win32con
        ex_tool = win32con.WS_EX_TOOLWINDOW
        gwl_exstyle = win32con.GWL_EXSTYLE
        gw_owner = win32con.GW_OWNER
    except Exception:
        ex_tool, gwl_exstyle, gw_owner = 0x00000080, -20, 4
    own = os.getpid()
    out = []

    def _cb(hwnd, _lparam):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetWindow(hwnd, gw_owner):       # owned dialog, not main win
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if not title.strip():
                return True
            try:
                cls = win32gui.GetClassName(hwnd) or ""
            except Exception:
                cls = ""
            if cls in _WIN_CONSOLE_CLASSES or cls in _WIN_SHELL_CLASSES:
                return True
            try:
                if win32gui.GetWindowLong(hwnd, gwl_exstyle) & ex_tool:
                    return True
            except Exception:
                pass
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            if not pid or pid == own:
                return True
            out.append((int(hwnd), title, int(pid), cls))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return out


def find_app_window(name):
    """(hwnd, title, pid) of the best ALREADY-OPEN window whose owning PROCESS name
    matches ``name`` (e.g. 'notepad' -> notepad.exe), or ``None`` when nothing
    matches confidently / detection is unavailable.

    Matches on the process name (titles rarely carry the app name) and requires an
    EXACT executable-base match so the wrong app is never focused; among several
    windows of one app the topmost (most-recently-active) is returned. Windows-only
    — returns None on Linux/Mac (caller launches as usual). Never raises."""
    if sys.platform != "win32" or _load_win32() is None:
        return None
    req = _norm_app_token(name)
    if not req:
        return None
    try:
        import psutil
    except Exception:
        return None
    for hwnd, title, pid, _cls in _enum_app_windows_win32():   # topmost first
        try:
            pname = psutil.Process(pid).name()
        except Exception:
            continue
        if _proc_base(pname) == req:
            return (hwnd, title, pid)
    return None


def _raise_window_win32(win32gui, win32process, hwnd) -> None:
    """Best-effort bring ``hwnd`` to the foreground, working around Windows'
    foreground lock (a background process can't normally steal focus) by briefly
    attaching our input thread to the current foreground window's thread. Never
    raises — if focus is refused, the caller's confirm-poll detects it."""
    try:
        import win32con
        sw_restore = win32con.SW_RESTORE
    except Exception:
        sw_restore = 9
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, sw_restore)
    except Exception:
        pass
    cur_tid = our_tid = 0
    attached = False
    try:
        import win32api
        our_tid = win32api.GetCurrentThreadId()
        fg = win32gui.GetForegroundWindow()
        if fg:
            cur_tid, _ = win32process.GetWindowThreadProcessId(fg)
        if our_tid and cur_tid and our_tid != cur_tid:
            try:
                win32process.AttachThreadInput(our_tid, cur_tid, True)
                attached = True
            except Exception:
                attached = False
    except Exception:
        pass
    for fn in ("BringWindowToTop", "SetForegroundWindow"):
        try:
            getattr(win32gui, fn)(hwnd)
        except Exception:
            pass
    if attached:
        try:
            win32process.AttachThreadInput(our_tid, cur_tid, False)
        except Exception:
            pass


def focus_app_window(window, timeout=APP_FOCUS_TIMEOUT,
                     poll=_APP_WINDOW_POLL, stable_polls=_APP_WINDOW_STABLE_POLLS):
    """Bring an already-open ``window`` ((hwnd, title, pid) from find_app_window) to
    the foreground and CONFIRM it took focus by polling, so a chained
    desktop_type/desktop_click is reliable. Returns a ``(status, detail)`` tuple:
      * ``("ready", <title>)`` — the window is now the foreground window (confirmed).
      * ``("timeout", "")``    — focus could not be confirmed in time -> the caller
        falls back to launching (safer than claiming success on an unfocused window).
      * ``("unavailable", "")``— focus not wired on this platform.
    Windows-only; never raises."""
    if sys.platform != "win32":
        return ("unavailable", "")
    w = _load_win32()
    if not w:
        return ("unavailable", "")
    win32gui, win32process = w
    try:
        hwnd = int(window[0])
    except Exception:
        return ("unavailable", "")
    _raise_window_win32(win32gui, win32process, hwnd)
    deadline = time.monotonic() + max(0.5, float(timeout))
    stable = 0
    while True:
        fg = _foreground_window_win32()
        if fg is None:
            return ("unavailable", "")
        fid, ftitle, _fpid = fg
        if fid == hwnd:
            stable += 1
            if stable >= stable_polls:
                title = (ftitle or (window[1] if len(window) > 1 else "")).strip()
                return ("ready", title)
        else:
            stable = 0
            _raise_window_win32(win32gui, win32process, hwnd)   # re-assert
        if time.monotonic() >= deadline:
            return ("timeout", "")
        time.sleep(poll)


# ---------------------------------------------------------------------------
# WOR-80 — Generic Windows app name -> executable resolution.
#
# subprocess.Popen(name, shell=True) is `cmd /c <name>`, which only finds apps on
# PATH. Per-user installs (Discord under %LocalAppData%, Steam/most games under
# Program Files, Store apps, …) are NOT on PATH, so a bare-name launch ALWAYS
# fails and OSIA only ever "worked" when the app already had a visible window to
# focus (tray/closed -> fail). resolve_windows_app() finds ANY installed app's
# real executable + arguments from its name, with NO per-app hardcoding, by
# consulting the same sources the shell / Start menu use, cheapest-and-most-
# reliable first:
#   1. App Paths registry  (HKLM + HKCU, incl. WOW6432Node) — many apps register
#      `<name>.exe` -> full path here.
#   2. Start menu .lnk scan (the general workhorse — covers nearly every installed
#      app) — fuzzy-match the requested name against every shortcut under the
#      per-user + all-users Start menu and resolve the best match's TargetPath +
#      Arguments (so a launcher shortcut like Discord's
#      `Update.exe --processStart Discord.exe` is honoured verbatim).
#   3. Common install roots — probe %LocalAppData% / %ProgramFiles% /
#      %ProgramFiles(x86)% for a matching folder/exe.
#   4. UWP / Store apps — Get-StartApps -> AppUserModelID, launched via
#      `explorer.exe shell:AppsFolder\<AUMID>`.
# The caller keeps its existing `cmd /c <name>` as tier 5 (PATH fallback), so apps
# already on PATH are unaffected. Windows-only: returns None on Linux/Mac (the
# caller keeps its current shell launch). Every function never raises.
# ---------------------------------------------------------------------------

# Launcher / shell-host process bases that are never "the app itself": excluded
# from window-focus + process-running matching so we never focus the desktop
# shell, claim a generic updater/installer stub as the launched app, or
# (critically) treat always-present explorer.exe as proof a UWP launch succeeded
# (which would break the anti-hallucination contract). Generic, not per-app.
_GENERIC_PROC_BASES = {
    "explorer", "cmd", "conhost", "powershell", "pwsh", "rundll32",
    "wscript", "cscript", "applicationframehost", "sihost", "dllhost",
    "svchost", "update", "updater", "setup", "install", "installer",
    "launcher", "stub", "bootstrap", "bootstrapper",
}

# A Start-menu / Store match below this fuzzy score is too weak to trust.
_FUZZY_MIN = 0.70


def _norm_alnum(s) -> str:
    """Lowercase, strip everything but [a-z0-9]: 'VS Code' -> 'vscode'."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _name_tokens(s):
    """Lowercase alphanumeric word tokens: 'Visual Studio Code' -> ['visual',
    'studio', 'code']."""
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def _initials(s) -> str:
    """First letter of each token: 'Visual Studio Code' -> 'vsc'."""
    return "".join(t[0] for t in _name_tokens(s))


# Exe / shortcut stem tokens that mark a helper, NOT the app the user wants —
# uninstallers, crash/error reporters, updaters, setup stubs. A sibling .exe whose
# name contains one of these is never chosen over the real app (WOR-80 review):
# 'discord' must not resolve to 'Uninstall Discord.exe', 'steam' not to
# 'steamerrorreporter.exe'. Generic, not per-app.
_HELPER_STEM_TOKENS = (
    "uninstall", "uninst", "unins", "remove", "setup", "install", "installer",
    "update", "updater", "upgrade", "repair", "crash", "error", "report",
    "reporter", "helper", "service", "daemon", "diagnostic", "cleanup",
)

# Narrower set for human-readable Start-menu LABELS: only the words that
# unambiguously mark a non-launch shortcut as a whole word (so 'Uninstall Discord'
# is skipped but 'Reporter Pro' / 'X Service' legit apps are kept).
_HELPER_LABEL_TOKENS = (
    "uninstall", "uninstaller", "remove", "setup", "installer", "repair",
)


def _is_helper_stem(stem) -> bool:
    """SUBSTRING test for an exe basename (glued names like 'steamerrorreporter',
    'DiscordSetup'): looks like an uninstaller / reporter / updater, not the app."""
    low = (stem or "").lower()
    return any(tok in low for tok in _HELPER_STEM_TOKENS)


def _is_helper_label(label) -> bool:
    """WHOLE-WORD test for a human-readable Start-menu shortcut stem ('Uninstall
    Discord'): a helper token must appear as its own word, so legit apps like
    'ServiceNow' / 'Reporter Pro' are not over-filtered."""
    toks = set(_name_tokens(label))
    return any(tok in toks for tok in _HELPER_LABEL_TOKENS)


def _acronym_seq_match(qtoks, ttoks) -> bool:
    """True when the query token sequence maps onto the target token sequence where
    each query token either equals a target token OR spells the initials of a run of
    consecutive target tokens — e.g. ['vs','code'] -> 'Visual Studio Code'
    (vs=Visual+Studio, code=Code). Requires at least one query token to be a FULL
    target word (an anchor) so a pure-initials coincidence ('git' ->
    'Google Input Tools') does NOT match. Specific, low false-positive."""
    if not qtoks or not ttoks:
        return False
    i = 0
    for qt in qtoks:
        if i >= len(ttoks):
            return False
        if qt == ttoks[i]:
            i += 1
            continue
        init, j = "", i                              # try a run whose initials == qt
        while j < len(ttoks) and len(init) < len(qt):
            init += ttoks[j][0]
            j += 1
            if init == qt:
                break
        if init == qt:
            i = j
            continue
        return False
    return any(qt in ttoks for qt in qtoks)          # needs a full-word anchor


def _fuzzy_score(query, target) -> float:
    """Heuristic 0..1 match of a requested app name against a candidate label (a
    Start-menu shortcut stem or a Store-app display name). Built to RESOLVE the
    real app (exact name, whole-word 'chrome'->'Google Chrome', acronym
    'vs code'->'Visual Studio Code') while NOT cross-resolving to a different
    product that merely shares a prefix ('word'->'WordPad', 'steam'->'SteamVR') —
    a wrong-app launch would be reported as success and break the
    anti-hallucination contract. Carries NO per-app knowledge. Higher is closer; a
    score below the caller's threshold means 'not confident, do not launch this'."""
    qraw = (query or "").strip().lower()
    traw = (target or "").strip().lower()
    q = _norm_alnum(query)
    t = _norm_alnum(target)
    if not q or not t:
        return 0.0
    if qraw == traw:                                 # true case-insensitive exact
        return 1.0
    if q == t:
        # Alnum-equal but the raw forms differ only by spacing/punctuation. High,
        # but BELOW a true exact, and penalised when the target dropped
        # identity-bearing symbols so 'notepad' does not tie 'Notepad' with
        # 'Notepad++' (review #5). Pure spacing differences ('VS Code') keep ~0.95.
        stripped = sum(1 for ch in traw if not ch.isalnum() and not ch.isspace())
        return max(0.80, 0.95 - 0.08 * stripped)
    qtoks = _name_tokens(query)
    ttoks = _name_tokens(target)
    score = 0.0
    # Whole-word: every query token is also a target token (order-independent).
    # SAFE — 'chrome'->'Google Chrome', 'obs'->'OBS Studio', 'word'->'Microsoft
    # Word' — and crucially 'word' is NOT a token of the single token 'wordpad'.
    if qtoks and all(qt in ttoks for qt in qtoks):
        score = max(score, 0.86)
    # Acronym sequence ('vs code' -> 'Visual Studio Code'), anchored on a full word.
    if len(qtoks) >= 2 and _acronym_seq_match(qtoks, ttoks):
        score = max(score, 0.84)
    # Single-token glued acronym ('vscode' -> 'vsc'+ode of Visual Studio Code):
    # the query must START WITH the target's initials AND spell out MORE than them
    # (len(q) > len(ini)), and be >= 4 chars. A bare initials-equality ('git' ==
    # 'Google Input Tools') is intentionally NOT accepted — that is a coincidence,
    # not an abbreviation — and short tokens cannot collide (review #1/#7).
    if len(qtoks) == 1:
        ini = _initials(target)
        if (ini and len(ini) >= 2 and len(q) >= 4
                and len(q) > len(ini) and q.startswith(ini)):
            score = max(score, 0.82)
    # Floor: difflib on the RAW forms, scaled so a weak similarity ('word' vs
    # 'wordpad' ~0.73) stays well under the launch threshold.
    score = max(score, difflib.SequenceMatcher(None, qraw, traw).ratio() * 0.65)
    return score


def _split_lnk_args(s):
    """Split a Windows shortcut argument string into a list, honouring quotes and
    preserving backslashes (so paths survive). Best-effort; never raises."""
    s = (s or "").strip()
    if not s:
        return []
    try:
        import shlex
        toks = shlex.split(s, posix=False)          # posix=False keeps backslashes
    except Exception:
        toks = s.split()
    out = []
    for tok in toks:
        if len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"':
            tok = tok[1:-1]
        out.append(tok)
    return out


def _read_lnk_target(path):
    """Resolve a .lnk shortcut to ``(target_exe, [args])`` via pywin32's COM
    WScript.Shell, or ``None`` when it has no usable filesystem target (e.g. a
    Store-app shortcut) or COM is unavailable. COM is initialised on the calling
    worker thread (the tool runs in a ThreadPoolExecutor), mirroring the existing
    UIA per-thread COM init. Never raises."""
    try:
        import pythoncom
        import win32com.client
    except Exception:
        return None
    initialized = False
    try:
        try:
            pythoncom.CoInitialize()
            initialized = True
        except Exception:
            initialized = False
        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(path)
        target = (sc.TargetPath or "").strip()
        if not target or not os.path.isfile(target):
            return None
        return (target, _split_lnk_args(sc.Arguments or ""))
    except Exception:
        return None
    finally:
        if initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _resolve_app_paths_registry(name):
    """Tier 1 — the App Paths registry (HKCU + HKLM + WOW6432Node). ``(path, [])``
    or None. Never raises."""
    try:
        import winreg
    except Exception:
        return None
    base = _norm_app_token(name)                    # 'discord' from 'Discord.exe'
    if not base:
        return None
    key_leaf = base + ".exe"
    locations = [
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for root, sub in locations:
        try:
            with winreg.OpenKey(root, sub + "\\" + key_leaf) as k:
                val, _typ = winreg.QueryValueEx(k, None)    # default value = exe
                path = (val or "").strip().strip('"')
                # App Paths values are often REG_EXPAND_SZ ('%ProgramFiles%\…');
                # winreg does NOT expand them, so expand before the isfile check
                # or a legitimately-registered app is missed (review #8).
                if path and _typ == winreg.REG_EXPAND_SZ:
                    path = os.path.expandvars(path)
                if path and os.path.isfile(path):
                    return (path, [])
        except OSError:
            continue
    return None


def _start_menu_dirs():
    """Per-user + all-users Start-menu Programs folders that exist."""
    out = []
    pd = os.environ.get("ProgramData")
    ad = os.environ.get("APPDATA")
    if pd:
        out.append(os.path.join(pd, "Microsoft", "Windows", "Start Menu", "Programs"))
    if ad:
        out.append(os.path.join(ad, "Microsoft", "Windows", "Start Menu", "Programs"))
    return [d for d in out if os.path.isdir(d)]


def _resolve_start_menu_lnk(name):
    """Tier 2 — the general workhorse. Fuzzy-match ``name`` against every shortcut
    stem under the Start menu, then COM-resolve the best matches' TargetPath +
    Arguments. ``(target, [args])`` or None. Never raises."""
    scored = []
    for d in _start_menu_dirs():
        try:
            walker = os.walk(d)
        except Exception:
            continue
        for root, _dirs, files in walker:
            for fn in files:
                if not fn.lower().endswith(".lnk"):
                    continue
                stem = fn[:-4]
                if _is_helper_label(stem):           # skip 'Uninstall X', 'Setup …'
                    continue
                sc = _fuzzy_score(name, stem)
                if sc >= _FUZZY_MIN:
                    scored.append((sc, len(_norm_alnum(stem)), os.path.join(root, fn)))
    if not scored:
        return None
    # Best score first; on ties prefer the shortest (closest-to-exact) label.
    scored.sort(key=lambda x: (-x[0], x[1]))
    for _sc, _ln, path in scored[:6]:
        resolved = _read_lnk_target(path)
        if resolved is not None:
            return resolved
    return None


def _find_exe_in_dir(folder, base):
    """Best exe inside ``folder`` for ``base`` (exact name, else a fuzzy-matching
    .exe, else one level of subfolders). Bounded + shallow. None if nothing fits."""
    cand = os.path.join(folder, base + ".exe")
    if os.path.isfile(cand):
        return cand
    best, best_sc = None, 0.0
    try:
        for entry in os.listdir(folder):
            if not entry.lower().endswith(".exe"):
                continue
            stem = entry[:-4]
            if _is_helper_stem(stem):                # never an uninstaller/reporter
                continue
            sc = _fuzzy_score(base, stem)
            if sc > best_sc:
                best, best_sc = os.path.join(folder, entry), sc
    except OSError:
        pass
    # Require a strong (whole-word/exact-grade) sibling match, not a weak fuzzy one,
    # so we never launch an unrelated exe that merely shares a few letters.
    if best and best_sc >= 0.86:
        return best
    try:
        for entry in os.listdir(folder):           # one level down: <folder>\*\<base>.exe
            sub = os.path.join(folder, entry)
            if os.path.isdir(sub):
                c = os.path.join(sub, base + ".exe")
                if os.path.isfile(c):
                    return c
    except OSError:
        pass
    return None


def _resolve_common_locations(name):
    """Tier 3 — probe the common per-user / all-users install roots for a folder or
    exe matching ``name``. ``(path, [])`` or None. Never raises."""
    base = _norm_app_token(name)
    if not base:
        return None
    roots = []
    for env in ("LocalAppData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(env)
        if v and os.path.isdir(v) and v not in roots:
            roots.append(v)
    for root in roots:
        direct = os.path.join(root, base + ".exe")
        if os.path.isfile(direct):
            return (direct, [])
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            if _fuzzy_score(name, entry) >= 0.85:
                exe = _find_exe_in_dir(full, base)
                if exe:
                    return (exe, [])
    return None


def _resolve_uwp(name):
    """Tier 4 — UWP / Store apps via PowerShell Get-StartApps. Returns
    ``("explorer.exe", ["shell:AppsFolder\\<AUMID>"])`` for the best name match, or
    None. Never raises."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 5s cap: this tier runs LAST (only when registry/Start-menu/common-paths
        # all miss, e.g. a typo'd / missing app), so it sits on the honest-ERROR
        # latency path — keep it short while staying well inside the 30s budget.
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=5, creationflags=flags,
        )
    except Exception:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]
    best, best_sc = None, 0.0
    for item in data:
        if not isinstance(item, dict):
            continue
        aid = item.get("AppID") or ""
        if not aid:
            continue
        sc = _fuzzy_score(name, item.get("Name") or "")
        if sc > best_sc:
            best, best_sc = aid, sc
    if best and best_sc >= _FUZZY_MIN:
        return ("explorer.exe", ["shell:AppsFolder\\" + best])
    return None


def resolve_windows_app(name):
    """Resolve an app ``name`` to ``(executable_path, [args])`` for a DIRECT launch,
    or None when no concrete target is found (the caller then keeps its `cmd /c
    <name>` PATH fallback). Tiers, cheapest/most-reliable first: an explicit
    existing path, App Paths registry, Start-menu .lnk scan, common install roots,
    UWP/Store. Windows-only; never raises. See the section header above."""
    if sys.platform != "win32":
        return None
    s = (name or "").strip()
    if not s:
        return None
    explicit = s.strip('"')
    if os.path.isabs(explicit) and os.path.isfile(explicit):
        return (explicit, [])
    for resolver in (_resolve_app_paths_registry,
                     _resolve_start_menu_lnk,
                     _resolve_common_locations,
                     _resolve_uwp):
        try:
            res = resolver(s)
        except Exception:
            res = None
        if res is not None:
            path, args = res
            if path:
                return (path, list(args or []))
    return None


def windows_app_candidates(name, resolved):
    """The set of process bases that identify THIS app's window / process — used to
    detect the launch reliably even when a bootstrapper hands off to a differently
    named process (e.g. Discord's Update.exe -> Discord.exe, 'vs code' -> Code.exe).
    Built from the raw name token, the resolved executable's base, and any .exe
    referenced in the resolved arguments. Generic launcher/host bases are dropped so
    they can never be mistaken for the app. Windows-only callers."""
    cands = set()
    tok = _norm_app_token(name)
    if tok:
        cands.add(tok)
    if resolved:
        path, args = resolved
        b = _proc_base(os.path.basename((path or "").replace("\\", "/")))
        if b:
            cands.add(b)
        for a in (args or []):
            a = str(a)
            if a.lower().endswith(".exe"):
                ab = _proc_base(os.path.basename(a.replace("\\", "/")))
                if ab:
                    cands.add(ab)
    return {c for c in cands if c and c not in _GENERIC_PROC_BASES}


def find_app_window_multi(candidates):
    """(hwnd, title, pid) of the topmost visible window whose owning PROCESS base is
    in ``candidates``, or None. Like :func:`find_app_window` but matches a SET of
    bases (the resolved-name candidates) instead of one raw token, so a window
    opened by a differently-named process than the user typed is still found.
    Windows-only; never raises."""
    if sys.platform != "win32" or _load_win32() is None:
        return None
    cands = {c for c in (candidates or set()) if c and c not in _GENERIC_PROC_BASES}
    if not cands:
        return None
    try:
        import psutil
    except Exception:
        return None
    for hwnd, title, pid, _cls in _enum_app_windows_win32():       # topmost first
        try:
            pname = psutil.Process(pid).name()
        except Exception:
            continue
        if _proc_base(pname) in cands:
            return (hwnd, title, pid)
    return None


def is_app_running(candidates):
    """True when a process whose base is in ``candidates`` is currently running —
    the signal that an app DID launch (heavy/cold app still drawing, launcher
    handed off, or app sitting in the tray) so the caller never false-ERRORs a real
    launch. Generic launcher/host bases are ignored. Never raises."""
    cands = {c for c in (candidates or set()) if c and c not in _GENERIC_PROC_BASES}
    if not cands:
        return False
    try:
        import psutil
    except Exception:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                if _proc_base(proc.info.get("name") or "") in cands:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# WOR-46 — Accessibility-API click backend (Windows UI Automation).
#
# resolve_element_rect() resolves a native UI element's EXACT bounding-rect
# center by walking the foreground window's accessibility tree (Windows UI
# Automation via the `uiautomation` library, which pulls comtypes). It is tried
# FIRST by agent.tool_desktop_click; on any miss it returns None and the caller
# falls back to the existing vision-guided locate. Pixel-exact coordinates mean
# the click needs no vision model and no 2-pass refinement.
#
# Cross-platform + safe by construction:
#   * Windows-only — returns None immediately on Linux/Mac/headless (sys.platform
#     guard), so the mobile server and the Linux GUI are pure no-ops.
#   * `uiautomation`/`comtypes` are imported LAZILY inside _load_uiautomation()
#     (never at module scope), mirroring the _load_pyautogui/_load_win32 guards —
#     a box without them is unaffected, and even on Windows the (heavy) import
#     only happens on the first UIA-backed click.
#   * COM is initialised per-call for the current thread via
#     UIAutomationInitializerInThread() (the agent runs on worker threads in BOTH
#     the GUI and the mobile-server process), and NO Control object is cached
#     across calls (stale-element safety).
#   * Bounded walk: shallow depth + node cap + a hard wall-clock deadline, so it
#     never stalls the click path on a huge tree — it just returns None and lets
#     vision take over.
#   * NEVER raises and NEVER returns an "ERROR:" string (the whole body is in
#     try/except → None), so the anti-hallucination tool contract is preserved
#     (None simply means "UIA could not resolve it — use vision").
# ---------------------------------------------------------------------------
_UIA_MAX_DEPTH = 6          # how deep below the foreground window we descend
_UIA_MAX_NODES = 300        # hard cap on elements inspected per call
_UIA_MIN_SCORE = 0.55       # below this a candidate is too weak to trust

# Control types that are typically the real click target — used only to break
# near-ties toward a clickable leaf over a same-named container (e.g. a Button
# nested in a Pane that carries the same Name).
_UIA_CLICKABLE_TYPES = frozenset({
    "ButtonControl", "MenuItemControl", "ListItemControl",
    "HyperlinkControl", "TextControl",
})

# Article / descriptor tokens stripped during normalisation, so "the Save button"
# / "le bouton Enregistrer" match an element simply named "Save" / "Enregistrer".
_UIA_STOPWORDS = frozenset({
    # articles (en / fr) — "l'" becomes the token "l" once punctuation is split
    "the", "a", "an", "le", "la", "les", "l", "un", "une", "des", "du", "de",
    # surrounding descriptor words (en / fr)
    "button", "bouton", "btn", "menu", "item", "icon", "icone", "icône",
    "link", "lien", "tab", "onglet", "field", "champ", "checkbox", "case",
})

_uiautomation = None
_uiautomation_loaded = False


def _load_uiautomation():
    """Import the ``uiautomation`` library once (Windows only), caching the module
    or ``None`` on failure. Lazy + cached, mirroring :func:`_load_pyautogui` /
    :func:`_load_win32`: the heavy comtypes-backed import happens only on the first
    UIA-backed click — never at module import — and a missing/broken package
    degrades gracefully to ``None`` (→ vision fallback)."""
    global _uiautomation, _uiautomation_loaded
    if _uiautomation_loaded:
        return _uiautomation
    _uiautomation_loaded = True
    if sys.platform != "win32":
        _uiautomation = None
        return None
    try:
        import uiautomation as _uia
        _uiautomation = _uia
    except Exception:
        _uiautomation = None
    return _uiautomation


def _normalize_label(text) -> str:
    """Lowercase ``text``, drop punctuation and article/descriptor stopwords, then
    collapse whitespace — so a loose target like "the Save button" reduces to the
    same token string as an element literally named "Save". Accented Latin letters
    (é, ç, …) are preserved so French labels survive."""
    s = text if isinstance(text, str) else str(text or "")
    # Any non-alphanumeric run -> space (handles "l'ouvrir", "OK!", "File/Edit").
    s = re.sub("[^0-9a-zA-ZÀ-ɏ]+", " ", s).lower()
    tokens = [t for t in s.split() if t and t not in _UIA_STOPWORDS]
    return " ".join(tokens)


def _label_score(target_norm, target_tokens, cand_text) -> float:
    """Fuzzy 0..1 match of a candidate element's text (Name or AutomationId)
    against the normalised target, favouring PRECISION — a UIA hit BYPASSES the
    vision fallback, so on any doubt we score low and let vision locate instead.

    Tiers: exact normalised match = 1.0; the full target present as a contiguous
    WORD phrase (e.g. "save" in "Save (Ctrl+S)", "save as" in "Save As…") = 0.9;
    all target tokens present as whole words but scattered/reordered = 0.8.
    Matching is strictly WORD-level — NEVER a raw character substring — so a short
    label like "file"/"ok" can no longer match "profile"/"book". A residual
    character ratio is returned only as a weak signal, deliberately scaled to stay
    BELOW ``_UIA_MIN_SCORE`` so a near-miss never bypasses vision on its own (a
    typo'd label simply falls through to the vision locate)."""
    cand_norm = _normalize_label(cand_text)
    if not cand_norm or not target_norm:
        return 0.0
    if cand_norm == target_norm:
        return 1.0
    cand_tokens = frozenset(cand_norm.split())
    if target_tokens and target_tokens <= cand_tokens:
        # Whole-word containment (word-boundary safe). A contiguous phrase
        # ("save" inside "save ctrl s") is stronger than scattered tokens.
        phrase = (" " + cand_norm + " ").find(" " + target_norm + " ") >= 0
        return 0.9 if phrase else 0.8
    # No whole-word match: a raw character ratio only, capped below the accept
    # threshold so it can never, by itself, short-circuit the vision path.
    ratio = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio()
    return round(ratio * 0.5, 4)


def _rect_center_if_valid(rect):
    """Integer center ``(cx, cy)`` of a UIA BoundingRectangle, or ``None`` when the
    rect is empty/zero-area or belongs to a minimised/off-screen window (which
    reports sentinel negatives around -32000).

    Merely-negative coordinates are NOT rejected: a real secondary monitor placed
    left of / above the primary uses small negative virtual-screen coordinates
    (e.g. -1920..0) that are legitimately clickable — UIA BoundingRectangle and
    pyautogui share that one signed coordinate space (unified by the WOR-46 DPI
    pin). Never raises."""
    try:
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
    except Exception:
        return None
    if right <= left or bottom <= top:          # empty / zero-area
        return None
    if left <= -10000 or top <= -10000:         # minimised-window sentinel (~ -32000)
        return None
    return ((left + right) // 2, (top + bottom) // 2)


def resolve_element_rect(target_label: str, timeout_s: float = 0.6) -> "tuple[int, int] | None":
    """Resolve the exact click point of the native UI element described by
    ``target_label`` via Windows UI Automation, returning its bounding-rect center
    ``(x, y)`` in physical screen pixels, or ``None`` when it cannot be resolved.

    Tried FIRST by ``agent.tool_desktop_click`` (accessibility-first); ``None``
    routes the caller to its existing vision-guided locate (graceful fallback).

    Returns ``None`` — fast, never raising — on every non-resolution path: not on
    Windows, ``uiautomation`` unavailable, no foreground window, no element whose
    Name/AutomationId fuzzy-matches, a COM error, an empty/zero/off-screen rect, or
    the wall-clock ``timeout_s`` budget elapsing before a match is found. It NEVER
    returns an ``"ERROR:"`` string, so the anti-hallucination contract holds and a
    headless / Linux / Mac call is a pure no-op.
    """
    if sys.platform != "win32":
        return None
    try:
        auto = _load_uiautomation()
        if auto is None:
            return None
        target_norm = _normalize_label(target_label)
        if not target_norm:
            return None
        target_tokens = frozenset(target_norm.split())
        deadline = time.monotonic() + max(0.1, float(timeout_s))

        # COM is initialised for THIS thread (the agent runs on a worker thread in
        # both the GUI and the mobile-server process). No Control is cached across
        # calls — every element below is freshly walked inside this block.
        with auto.UIAutomationInitializerInThread():
            fg = auto.GetForegroundControl()
            if not fg:
                return None
            try:
                children = fg.GetChildren()
            except Exception:
                children = []
            # Breadth-first, shallow. The foreground WINDOW itself is never a click
            # target (you click an element INSIDE it), so we seed the frontier with
            # its children at depth 1 and score outward from there.
            frontier = [(c, 1) for c in children]
            best_key = -1.0
            best_center = None
            visited = 0
            while frontier:
                if time.monotonic() >= deadline or visited >= _UIA_MAX_NODES:
                    break
                ctrl, depth = frontier.pop(0)
                visited += 1
                try:
                    name = ctrl.Name or ""
                except Exception:
                    name = ""
                try:
                    aid = ctrl.AutomationId or ""
                except Exception:
                    aid = ""
                # Match Name first; only consult AutomationId if Name was too weak.
                score = _label_score(target_norm, target_tokens, name)
                if score < _UIA_MIN_SCORE and aid:
                    score = max(score, _label_score(target_norm, target_tokens, aid))
                if score >= _UIA_MIN_SCORE:
                    try:
                        ctype = ctrl.ControlTypeName or ""
                    except Exception:
                        ctype = ""
                    key = score + (0.05 if ctype in _UIA_CLICKABLE_TYPES else 0.0)
                    if key > best_key:
                        try:
                            rect = ctrl.BoundingRectangle
                        except Exception:
                            rect = None
                        center = _rect_center_if_valid(rect) if rect is not None else None
                        if center is not None:
                            best_key, best_center = key, center
                if depth < _UIA_MAX_DEPTH:
                    try:
                        kids = ctrl.GetChildren()
                    except Exception:
                        kids = []
                    for ch in kids:
                        frontier.append((ch, depth + 1))
            return best_center
    except Exception:
        return None


# ---------------------------------------------------------------------------
# WOR-90 — KEYBOARD-NAVIGATION activation tier (Windows-only).
#
# A deterministic tier BETWEEN UIA-direct-click (WOR-46 resolve_element_rect) and
# the vision fallback. When the target cannot be resolved to a clickable rect
# (a control UIA exposes by Name but without a usable BoundingRectangle, or a
# list / menu / tree ITEM reached by arrowing rather than by a point), drive
# the keyboard the way a human would: Tab / Shift+Tab between focusable controls
# (and the arrows within an already-open composite), reading the FOCUSED element via UI
# Automation after each keypress, and ACTIVATE (Enter, or Space for a toggle) the
# one whose Name / AutomationId fuzzy-matches the request.
#
# Hierarchy: deep-link -> DOM (WOR-84) -> UIA-direct (WOR-46) -> KEYBOARD-NAV
# (here) -> vision (last resort). Safe by construction, exactly like the two tiers
# it sits between:
#   * Windows-only — returns None immediately on Linux / Mac / headless, so the
#     mobile server and the Linux GUI are pure no-ops (-> vision everywhere else).
#   * Best-effort — NEVER raises and NEVER returns an "ERROR:" string. It returns
#     a success string only after a VERIFIED activation, otherwise None so the
#     caller falls through to vision UNCHANGED (anti-hallucination contract held).
#   * It ACTIVATES rather than returning coordinates, so it runs its OWN
#     BEFORE/AFTER screen-diff (reusing agent._capture_screen / _screens_changed):
#     the BEFORE frame is captured IMMEDIATELY before the activation keypress so
#     the diff isolates the activation's effect from the Tab focus-ring movement —
#     an activation that changes nothing is reported as a MISS (-> vision), never a
#     false success.
#   * Positive identification: it only sends keys when the foreground window AND
#     the focused control belong to the intended app and NOT to OSIA itself (pid
#     checks, mirroring WOR-84), so it can never Tab-spam OSIA's own popup.
#   * Bounded: hard caps on Tab / arrow presses, plus "focus didn't move" and
#     "cycled back to start" aborts, so it can never loop forever.
# ---------------------------------------------------------------------------
_KBD_MAX_TABS = 40            # hard cap on Tab presses while scanning the focus ring
_KBD_MAX_ARROWS = 30          # hard cap on arrow presses within a composite control
_KBD_SETTLE_S = 0.06          # let focus settle after each navigation keypress (~60ms)
_KBD_ACTIVATE_SETTLE_S = 0.2  # let the UI react after an activation before re-capturing

# Focused control types activated with SPACE (a confirmed checkbox / toggle).
# Everything else clickable (button / link / menu item / tab / list item) uses
# ENTER — Space only ever toggles a confirmed toggle, never "clicks" a button.
_KBD_TOGGLE_TYPES = frozenset({"CheckBoxControl", "RadioButtonControl"})

# Composite control types whose ITEMS are reached by the ARROWS rather than Tab,
# mapped (via _kbd_arrow_keys) to the (forward, backward) arrow pair used to sweep
# them: vertical lists/menus/trees -> Down/Up; a menu bar -> Right/Left. These move
# a SELECTION HIGHLIGHT between named items and only COMMIT on Enter, which fits the
# navigate-then-activate model and leaves the screen essentially unchanged on a miss.
# Deliberately EXCLUDED: SliderControl (Right/Left changes the value, no matchable
# items) and TabControl/TabItemControl (Right/Left switches the active page) — both
# would mutate state with no upside; and ComboBoxControl (would require OPENING the
# dropdown — a screen mutation — and a classic Win32 combobox keeps focus on itself
# rather than exposing focusable options). All of these fall to the Tab ring / vision.
_KBD_VERTICAL_TYPES = frozenset({
    "ListControl", "ListItemControl", "MenuControl", "MenuItemControl",
    "TreeControl", "TreeItemControl",
})
_KBD_HORIZONTAL_TYPES = frozenset({"MenuBarControl"})

# Control types this tier is allowed to ACTIVATE (press Enter / Space on). A loose
# fuzzy match on anything else (an EditControl, a static TextControl, a PaneControl,
# a bare ListControl container, ...) must NOT be activated: e.g. Enter in a focused
# text field inserts a newline, which would change the screen and be misread as a
# successful activation. Mirrors the spec's "Enter for button/link/menuitem" intent.
_KBD_ACTIVATABLE_TYPES = frozenset({
    "ButtonControl", "SplitButtonControl", "HyperlinkControl",
    "MenuItemControl", "ListItemControl", "TreeItemControl", "TabItemControl",
    "CheckBoxControl", "RadioButtonControl",
})

# Lightweight value object for the keyboard-focused element. ``rect`` is the
# validated bounding-rect CENTER (x, y) or None — used only as part of the focus
# identity for the anti-loop guards, never to click.
_FocusedElement = namedtuple(
    "_FocusedElement",
    "name automation_id control_type_name rect hwnd pid",
)


def get_focused_element():
    """Return the currently keyboard-focused UI element as a ``_FocusedElement``
    ``(name, automation_id, control_type_name, rect, hwnd, pid)``, or ``None`` on
    any failure, when not on Windows, or when ``uiautomation`` is unavailable.

    ``rect`` is the element's validated bounding-rect CENTER ``(cx, cy)`` (via
    :func:`_rect_center_if_valid`, so an empty / off-screen rect yields ``None``);
    it is used only as part of the focus identity for the anti-loop guards in
    :func:`activate_by_keyboard`, never to click. Mirrors
    :func:`resolve_element_rect`'s safety verbatim: Windows-only, lazy+cached
    ``uiautomation``, per-thread COM via ``UIAutomationInitializerInThread`` and no
    Control cached across calls — and it NEVER raises (the whole body is wrapped in
    try/except -> ``None``)."""
    if sys.platform != "win32":
        return None
    auto = _load_uiautomation()
    if auto is None:
        return None
    try:
        # COM is initialised for THIS thread (the agent runs on a worker thread in
        # both the GUI and the mobile-server process); no Control is cached.
        with auto.UIAutomationInitializerInThread():
            ctrl = auto.GetFocusedControl()
            if not ctrl:
                return None
            try:
                name = ctrl.Name or ""
            except Exception:
                name = ""
            try:
                aid = ctrl.AutomationId or ""
            except Exception:
                aid = ""
            try:
                ctype = ctrl.ControlTypeName or ""
            except Exception:
                ctype = ""
            try:
                rect = _rect_center_if_valid(ctrl.BoundingRectangle)
            except Exception:
                rect = None
            try:
                hwnd = int(ctrl.NativeWindowHandle or 0)
            except Exception:
                hwnd = 0
            try:
                pid = int(ctrl.ProcessId or 0)
            except Exception:
                pid = 0
            return _FocusedElement(name, aid, ctype, rect, hwnd, pid)
    except Exception:
        return None


def _kbd_identity(foc):
    """Opaque identity of a focused element for the "focus moved?" anti-loop
    guards: window handle + validated rect-center + Name + AutomationId + control
    type. All five fields are stable across re-reads of the SAME control, so a Tab
    that does not move focus re-reads the same identity; including AutomationId and
    the control type (both already populated, no extra COM cost) keeps two DISTINCT
    nameless/rectless controls (hwnd 0, rect None, name "") from being conflated and
    aborting the sweep prematurely."""
    return (foc.hwnd, foc.rect, foc.name, foc.automation_id, foc.control_type_name)


def _kbd_match_score(target_norm, target_tokens, foc) -> float:
    """Best fuzzy 0..1 score of a focused element against the normalised target —
    its Name first, then its AutomationId only when the Name was too weak (mirrors
    :func:`resolve_element_rect`'s Name-then-AutomationId scoring, reusing
    :func:`_label_score`'s word-level precision so a doubtful match stays below
    ``_UIA_MIN_SCORE`` and falls to vision instead of pressing Enter blindly).

    Returns ``0.0`` for any control type this tier must not activate (text fields,
    static text, panes, bare list/menu containers): pressing Enter on those would
    either do nothing or, worse, edit content (a newline in a text field) that the
    screen-diff would misread as a successful activation."""
    if foc.control_type_name not in _KBD_ACTIVATABLE_TYPES:
        return 0.0
    score = _label_score(target_norm, target_tokens, foc.name)
    if score < _UIA_MIN_SCORE and foc.automation_id:
        score = max(score, _label_score(target_norm, target_tokens, foc.automation_id))
    return score


def _kbd_arrow_keys(control_type_name):
    """(forward, backward) arrow pair to sweep an ALREADY-OPEN composite control's
    items, or ``None`` when the focused control is not arrow-navigable (-> use the
    Tab ring). Only controls whose arrows move a selection HIGHLIGHT between named
    items (committed on Enter) are handled here."""
    if control_type_name in _KBD_VERTICAL_TYPES:
        return ("down", "up")
    if control_type_name in _KBD_HORIZONTAL_TYPES:
        return ("right", "left")
    return None


def activate_by_keyboard(description: str) -> "str | None":
    """Best-effort keyboard activation of the native element described by
    ``description`` (Windows-only). Returns a short SUCCESS string after a verified
    activation, or ``None`` on EVERY non-resolution path — not on Windows,
    ``uiautomation`` unavailable, no confident foreground / focus target, no
    fuzzy-matched control, a focus loop, a no-op (unverified) activation, or any
    error. ``None`` routes :func:`agent.tool_desktop_click` to its vision fallback.

    It NEVER raises and NEVER returns an ``"ERROR:"`` string, so the
    anti-hallucination tool contract holds and a Linux / Mac / headless call is a
    pure no-op. Unlike :func:`resolve_element_rect` it ACTIVATES the target
    (presses Enter, or Space for a confirmed toggle) instead of returning
    coordinates, so it owns its BEFORE/AFTER screen-diff verification."""
    if sys.platform != "win32" or _load_uiautomation() is None:
        return None
    try:
        target_norm = _normalize_label(description)
        if not target_norm:
            return None
        target_tokens = frozenset(target_norm.split())
        own = os.getpid()

        # Foreground-is-intended-app guard: require a real, titled, visible
        # foreground window owned by a process that is NOT OSIA's own. Mirrors
        # wait_for_app_window's is_new_app test (minus the before-id). Without a
        # confident foreground target we never send keys (-> vision).
        fg = _foreground_window()
        if not fg:
            return None
        fg_id, fg_title, fg_pid = fg
        if not fg_id or not str(fg_title).strip() or not fg_pid or fg_pid == own:
            return None

        # Reuse the agent's capture + pure-local screen-diff for the activation
        # verify. Lazy import: this Windows-only click path only ever runs well
        # after osia.agent is fully imported (agent.tool_desktop_click is the sole
        # caller), so there is no import cycle.
        try:
            from osia.agent import _capture_screen, _screens_changed
        except Exception:
            return None

        before = os.path.join(tempfile.gettempdir(), "osia_kbd_before.png")
        after = os.path.join(tempfile.gettempdir(), "osia_kbd_after.png")

        def _confirmed(foc):
            # Positive identification (WOR-84 style): a real focused control that
            # lives in the foreground, non-OSIA app — never OSIA's own window.
            return (bool(foc) and bool(foc.pid)
                    and foc.pid != own and foc.pid == fg_pid)

        def _activate(foc):
            """Capture BEFORE, press the activation key, capture AFTER, and return a
            SUCCESS string iff the screen visibly changed — else ``None`` (the
            activation was a no-op, i.e. a MISS, so the caller falls to vision).
            Space ONLY for a confirmed checkbox/toggle; Enter for everything else.
            The BEFORE frame is captured here, immediately before the keypress, so
            the diff isolates the activation from the Tab focus-ring movement."""
            key = "space" if foc.control_type_name in _KBD_TOGGLE_TYPES else "enter"
            if _capture_screen(before) is not None:   # cannot verify -> don't risk it
                return None
            # Re-confirm focus AFTER the (latency-heavy) full-screen BEFORE capture
            # and IMMEDIATELY before the keypress: press_key targets whatever the OS
            # currently focuses, and focus could have moved during the capture (a
            # toast / dialog stealing it). If focus is no longer confidently on the
            # very control we matched, abort to vision rather than send a key blind.
            recheck = get_focused_element()
            if not _confirmed(recheck) or _kbd_identity(recheck) != _kbd_identity(foc):
                return None
            r = press_key(key)
            if isinstance(r, str) and r.upper().startswith("ERROR"):
                return None
            time.sleep(_KBD_ACTIVATE_SETTLE_S)
            if _capture_screen(after) is not None:
                return None
            if _screens_changed(before, after):
                label = foc.name or foc.control_type_name or description
                return f'Activated "{description}" by keyboard ({key} on "{label}").'
            return None                               # no visible change -> MISS

        def _sweep(fwd, bwd, max_steps):
            """Sweep the focus ring with ``fwd`` (Tab / Down / Right), tracking the
            BEST fuzzy match. An EXACT match is activated on the spot; otherwise the
            best above-threshold match is recovered with ``bwd`` (Shift+Tab / Up /
            Left) and activated. Bounded by ``max_steps`` plus "focus didn't move"
            and "cycled back to start" aborts. Returns a success string or None.

            Why best-of-ring rather than first-above-threshold: activation is
            irreversible, and a loose request ("save") matches both "Save" (1.0) and
            "Save As" (0.9) — picking the best avoids pressing Enter on the wrong
            control."""
            foc = get_focused_element()
            if not _confirmed(foc):
                return None
            start_id = _kbd_identity(foc)
            best_score = _kbd_match_score(target_norm, target_tokens, foc)
            best_id = start_id
            if best_score >= 0.999:                   # exact at the start -> done
                return _activate(foc)
            prev_id = start_id
            for _ in range(max_steps):
                r = press_key(fwd)
                if isinstance(r, str) and r.upper().startswith("ERROR"):
                    return None
                time.sleep(_KBD_SETTLE_S)
                foc = get_focused_element()
                if not _confirmed(foc):
                    break
                cur_id = _kbd_identity(foc)
                if cur_id == prev_id:                 # focus didn't move -> stuck/end
                    break
                if cur_id == start_id:                # full ring traversed
                    break
                score = _kbd_match_score(target_norm, target_tokens, foc)
                if score >= 0.999:                    # exact -> activate now
                    return _activate(foc)
                if score > best_score:
                    best_score, best_id = score, cur_id
                prev_id = cur_id
            if best_score < _UIA_MIN_SCORE:
                return None
            # Overshoot recovery: the best match was passed during the forward
            # sweep — step back toward it and activate once focus lands on it again
            # (identity match, bounded, with its own "focus didn't move" abort).
            foc = get_focused_element()
            if foc is not None and _confirmed(foc) and _kbd_identity(foc) == best_id:
                return _activate(foc)
            prev_id = _kbd_identity(foc) if foc is not None else None
            for _ in range(max_steps + 3):
                r = press_key(bwd)
                if isinstance(r, str) and r.upper().startswith("ERROR"):
                    return None
                time.sleep(_KBD_SETTLE_S)
                foc = get_focused_element()
                if not _confirmed(foc):
                    return None
                cur_id = _kbd_identity(foc)
                if cur_id == best_id:
                    return _activate(foc)
                if cur_id == prev_id:                 # backward focus stuck -> give up
                    return None
                prev_id = cur_id
            return None

        # Keyboard focus must already sit inside the intended foreground, non-OSIA
        # app before we drive the keyboard (positive identification). If not, the
        # focus is somewhere we must not Tab-spam -> fall to vision.
        first = get_focused_element()
        if not _confirmed(first):
            return None

        # (A) In-element navigation when focus already rests on a composite control
        # whose items are reached by the ARROWS (an already-open menu / list / tree,
        # or a menu bar). The arrows only move a selection HIGHLIGHT between named
        # items (Enter commits), so a miss leaves the screen essentially as we found
        # it; on a miss we go straight to vision rather than Tab-spamming out of the
        # composite. We deliberately do NOT open closed comboboxes here (opening
        # mutates the screen, and a focus-retaining classic combobox can't be matched
        # item-by-item) — a focused combobox falls to the Tab ring / vision instead.
        arrows = _kbd_arrow_keys(first.control_type_name)
        if arrows:
            return _sweep(arrows[0], arrows[1], _KBD_MAX_ARROWS)

        # (B) Tab ring across the top-level focusable controls.
        return _sweep("tab", "shift+tab", _KBD_MAX_TABS)
    except Exception:
        return None
