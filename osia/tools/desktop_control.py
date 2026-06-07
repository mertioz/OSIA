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

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

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
