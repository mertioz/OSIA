#!/usr/bin/env python3
"""Steam install-state reader for OSIA (WOR-87; also closes WOR-85's
name->appid follow-up).

A DETERMINISTIC, no-vision answer to "is this Steam game installed / running?",
read straight from Steam's own on-disk bookkeeping instead of guessing or taking
a screenshot. The chain:

  1. Find Steam's install root — on Windows the authoritative
     ``HKCU\\Software\\Valve\\Steam`` ``SteamPath`` value (``winreg``, the same
     registry-reader pattern ``desktop_control`` uses for App Paths); Linux/Mac
     fall back to the well-known per-user Steam directories.
  2. Parse ``<steam>/steamapps/libraryfolders.vdf`` for EVERY library path (games
     are routinely installed on a second drive), plus the main install dir.
  3. Parse each ``<library>/steamapps/appmanifest_*.acf`` (plain Valve key-value
     text — no external library needed) for ``{appid, name, installdir,
     StateFlags}``. ``StateFlags & 4`` == ``StateFullyInstalled`` == fully
     downloaded and ready to launch.

Public surface:
  * :func:`steam_path`        -> Steam's install root, or ``None``.
  * :func:`steam_game_state`  -> ``{appid, name, installed, running, installdir}``
                                 for the best fuzzy name (or appid) match, or
                                 ``None`` when Steam isn't installed / has no
                                 manifests / nothing matches.

Robust by construction: every registry / filesystem / parse step is wrapped so a
missing Steam, an unreadable manifest, or malformed VDF returns ``None``/empty
instead of raising. Cross-platform: the registry read is ``sys.platform``-gated;
Linux/Mac use the XDG / Application-Support Steam locations. Pure stdlib.
"""

from __future__ import annotations

import os
import re
import sys


# ── Steam StateFlags bitmask (the bits we care about) ──────────────────────
# Steam records each app's install state as a bitmask in its appmanifest.
STATE_FULLY_INSTALLED = 4      # StateFullyInstalled — installed and ready to play
STATE_APP_RUNNING = 64         # StateAppRunning — Steam thinks the game is running


def _read_text(path: str):
    """Read a (small) text file as UTF-8, tolerant of stray bytes. ``None`` on
    any error so callers degrade gracefully."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def _norm_alnum(s: str) -> str:
    """Collapse a string to lowercase alphanumerics ('ELDEN RING' -> 'eldenring')."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def steam_path():
    """Steam's install root, or ``None`` when Steam isn't found. Never raises.

    Windows: the authoritative ``Software\\Valve\\Steam`` ``SteamPath`` value
    (winreg, HKCU then HKLM / WOW6432Node) — this respects a custom install
    drive. Linux/Mac: the well-known per-user Steam directories. Each candidate
    is confirmed to actually exist before being returned.
    """
    if sys.platform == "win32":
        try:
            import winreg
        except Exception:
            return None
        locations = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
        ]
        for root, sub in locations:
            try:
                with winreg.OpenKey(root, sub) as k:
                    for val_name in ("SteamPath", "InstallPath"):
                        try:
                            val, _typ = winreg.QueryValueEx(k, val_name)
                        except OSError:
                            continue
                        p = (val or "").strip().strip('"')
                        if not p:
                            continue
                        if _typ == winreg.REG_EXPAND_SZ:
                            p = os.path.expandvars(p)
                        p = os.path.normpath(p)
                        if os.path.isdir(p):
                            return p
            except OSError:
                continue
            except Exception:
                continue
        return None

    # Linux / Mac — probe the canonical per-user install dirs.
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        cands = [os.path.join(home, "Library", "Application Support", "Steam")]
    else:
        cands = [
            os.path.join(home, ".steam", "steam"),
            os.path.join(home, ".local", "share", "Steam"),
            os.path.join(home, ".steam", "root"),
            # Flatpak install
            os.path.join(home, ".var", "app", "com.valvesoftware.Steam",
                         "data", "Steam"),
        ]
    for p in cands:
        try:
            if os.path.isdir(p):
                return p
        except Exception:
            continue
    return None


def _parse_vdf(text: str) -> dict:
    """Minimal Valve KV (VDF/ACF) text -> nested ``dict`` (keys lowercased).

    Handles quoted keys/values and nested ``{ }`` blocks; ignores everything
    else (comments, ``#include`` lines). Tolerant: malformed input yields
    whatever parsed so far, never raises. Scalar values stay as strings (original
    case preserved — they carry paths and game names); sub-objects are dicts.
    """
    out: dict = {}
    stack = [out]
    pending_key = None
    # A token is either a quoted string (group 1, escapes honoured) or a brace.
    token_re = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')
    for m in token_re.finditer(text or ""):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            d: dict = {}
            if pending_key is not None:
                stack[-1][pending_key] = d
                pending_key = None
            stack.append(d)
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            pending_key = None
        else:
            s = s.replace("\\\\", "\\").replace('\\"', '"')
            if pending_key is None:
                pending_key = s.lower()
            else:
                stack[-1][pending_key] = s
                pending_key = None
    return out


def _library_dirs(steam_root: str) -> list:
    """Every Steam library root: the main install dir plus each entry in
    ``libraryfolders.vdf`` (games often live on a second drive). Deduped,
    normalised, existing-dir-not-required (the caller probes ``steamapps``
    itself). Always includes ``steam_root`` so a default install with no extra
    libraries still works."""
    libs: list = []
    seen = set()

    def _add(p):
        if not p:
            return
        try:
            ap = os.path.normpath(str(p))
        except Exception:
            return
        key = ap.lower()
        if key in seen:
            return
        seen.add(key)
        libs.append(ap)

    _add(steam_root)
    for vp in (os.path.join(steam_root, "steamapps", "libraryfolders.vdf"),
               os.path.join(steam_root, "config", "libraryfolders.vdf")):
        if not os.path.isfile(vp):
            continue
        text = _read_text(vp)
        if not text:
            continue
        try:
            data = _parse_vdf(text)
        except Exception:
            continue
        lf = data.get("libraryfolders")
        if not isinstance(lf, dict):
            continue
        for _k, v in lf.items():
            if isinstance(v, dict):
                _add(v.get("path"))
            elif isinstance(v, str) and str(_k).isdigit():
                # Legacy format: a numeric library slot maps straight to a path
                # ("0" "C:\\path"). Only numeric-keyed scalars are library paths;
                # bookkeeping scalars (TimeNextStatsReport, ContentStatsID) share
                # this level and must NOT be treated as roots.
                _add(v)
        break  # first readable libraryfolders.vdf wins
    return libs


def _manifest_record(app: dict, library: str):
    """Build a normalised record from a parsed ``AppState`` dict, or ``None``
    when it lacks an appid."""
    if not isinstance(app, dict):
        return None
    appid = (app.get("appid") or "").strip()
    if not appid:
        return None
    try:
        flags = int((app.get("stateflags") or "0").strip())
    except (TypeError, ValueError):
        flags = 0
    record = {
        "appid": appid,
        "name": (app.get("name") or "").strip(),
        "installdir": (app.get("installdir") or "").strip(),
        "stateflags": flags,
        "library": library,
    }
    try:
        total = int(app.get("bytestodownload", ""))
        downloaded = int(app.get("bytesdownloaded", ""))
    except (TypeError, ValueError):
        return record
    if total > 0 and 0 <= downloaded <= total:
        record.update(bytes_to_download=total, bytes_downloaded=downloaded)
    return record


def _iter_installed_games(steam_root: str):
    """Yield a normalised record per ``appmanifest_*.acf`` found across all
    libraries. Best-effort: skips any manifest it can't read or parse."""
    for lib in _library_dirs(steam_root):
        apps_dir = os.path.join(lib, "steamapps")
        try:
            entries = os.listdir(apps_dir)
        except Exception:
            continue
        for fn in entries:
            if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                continue
            text = _read_text(os.path.join(apps_dir, fn))
            if not text:
                continue
            try:
                data = _parse_vdf(text)
            except Exception:
                continue
            rec = _manifest_record(data.get("appstate"), lib)
            if rec is not None:
                yield rec


def _find_manifest_by_appid(steam_root: str, appid: str):
    """Fast path for a numeric appid: open ``appmanifest_<appid>.acf`` directly
    in each library instead of scanning them all. ``None`` if not present."""
    for lib in _library_dirs(steam_root):
        p = os.path.join(lib, "steamapps", f"appmanifest_{appid}.acf")
        if not os.path.isfile(p):
            continue
        text = _read_text(p)
        if not text:
            continue
        try:
            data = _parse_vdf(text)
        except Exception:
            continue
        rec = _manifest_record(data.get("appstate"), lib)
        if rec is not None:
            return rec
    return None


def _game_proc_candidates(name: str, installdir: str) -> set:
    """Best-effort set of process bases that would identify THIS game's running
    process — the alnum-collapsed game name and install-dir basename (e.g.
    'ELDEN RING' / installdir 'ELDEN RING' -> {'eldenring'}). Used only to ASK
    ``is_app_running``; a miss just means ``running=False`` (honest), never a
    wrong claim. Short tokens (< 4 chars) are dropped so they can't collide with
    an unrelated process."""
    cands = set()
    base = os.path.basename((installdir or "").rstrip("/\\"))
    for raw in (name, base):
        a = _norm_alnum(raw)
        if len(a) >= 4:
            cands.add(a)
    return cands


def _game_running(rec: dict) -> bool:
    """True when the game described by ``rec`` looks like it is currently
    running. Primary signal: a live process whose base matches the game's
    name / install dir (psutil, via desktop_control.is_app_running). Secondary:
    Steam's own ``StateAppRunning`` flag in the manifest (covers games whose exe
    name we couldn't guess). Best-effort; never raises."""
    try:
        from osia.tools import desktop_control as _dc
        cands = _game_proc_candidates(rec.get("name", ""), rec.get("installdir", ""))
        if cands and _dc.is_app_running(cands):
            return True
    except Exception:
        pass
    return bool(rec.get("stateflags", 0) & STATE_APP_RUNNING)


def steam_game_state(name):
    """Deterministic install/run state of the Steam game best matching ``name``.

    Returns ``{appid, name, installed, running, installdir}`` for the best match
    among installed appmanifests, or ``None`` when Steam isn't installed, has no
    manifests, or nothing matches ``name`` confidently. Includes the Steam
    manifest's byte counters when present and consistent. Never raises.

      * ``installed`` — the manifest's StateFlags has bit 4 (StateFullyInstalled):
        the game's files are fully installed on disk and launchable. A pending
        content update (StateUpdateRequired, bit 2) may still be set alongside it,
        in which case Steam applies the update on launch — still reported as
        installed because the game is present and playable.
      * ``running``   — a process matching the game's name / install dir is alive
        (best-effort), or Steam's manifest marks it running.

    ``name`` may be a human title ('Elden Ring') or an appid string ('1245620').
    A numeric query is matched as an appid first (fast/exact); if no manifest has
    that appid it falls back to a fuzzy TITLE match, so a game whose title is
    itself numeric ('2048') is still found. Fuzzy matching reuses
    desktop_control's resolver scorer so the threshold matches the rest of OSIA.
    """
    q = (name or "").strip()
    if not q:
        return None
    root = steam_path()
    if not root:
        return None

    best = None
    if q.isdigit():
        # Numeric query -> open that appid's manifest directly (fast, exact). On a
        # miss we DON'T return early: fall through to the fuzzy title scan below so
        # a numeric-TITLED game ('2048') is still matched.
        try:
            best = _find_manifest_by_appid(root, q)
        except Exception:
            best = None
    if best is None:
        # Title (or a numeric query that matched no appid) -> fuzzy match across
        # all installed manifests.
        try:
            games = list(_iter_installed_games(root))
        except Exception:
            games = []
        if not games:
            return None
        try:
            from osia.tools import desktop_control as _dc
            scorer, threshold = _dc._fuzzy_score, _dc._FUZZY_MIN
        except Exception:
            scorer, threshold = None, 0.70
        best_sc = 0.0
        for g in games:
            nm = g.get("name") or ""
            if not nm:
                continue
            if scorer is not None:
                try:
                    sc = scorer(q, nm)
                except Exception:
                    sc = 0.0
            else:
                qa, na = _norm_alnum(q), _norm_alnum(nm)
                if qa and qa == na:
                    sc = 1.0
                elif qa and (qa in na or na in qa):
                    sc = 0.8
                else:
                    sc = 0.0
            if sc > best_sc:
                best, best_sc = g, sc
        if best is None or best_sc < threshold:
            return None

    return {
        "appid": best["appid"],
        "name": best["name"],
        "installed": bool(best["stateflags"] & STATE_FULLY_INSTALLED),
        "running": _game_running(best),
        "installdir": best["installdir"],
        **{key: best[key] for key in ("bytes_to_download", "bytes_downloaded") if key in best},
    }
