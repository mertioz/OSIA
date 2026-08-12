#!/usr/bin/env python3
"""
OSIA tools package — startup health check for the major tool categories.
"""

import os
import shutil
import sys

from osia.utils.subprocess_policy import run_background_probe


def verify_tools() -> dict:
    """Run a quick health check of each major tool category and log which
    are available vs unavailable. Called once at startup.

    Checks (all side-effect free):
      - bash:      a trivial shell command runs and returns its output
      - file_read: a known file can be opened and read from disk
      - browser:   a CDP-capable browser binary is installed AND the
                   browser tooling (playwright) imports cleanly

    Returns a dict mapping category name -> bool. Never raises — a failed
    check just marks that category unavailable.
    """
    results: dict = {}

    # bash ------------------------------------------------------------------
    try:
        if sys.platform == "win32":
            # No bash on Windows — probe PowerShell instead.
            if shutil.which("powershell"):
                results["bash"] = True
            else:
                proc = run_background_probe(
                    ["powershell.exe", "-NoProfile", "-Command",
                     "echo osia-health-check"],
                    capture_output=True, text=True, timeout=5,
                )
                results["bash"] = (
                    proc.returncode == 0
                    and proc.stdout.strip() == "osia-health-check"
                )
        else:
            proc = run_background_probe(
                ["bash", "-c", "echo osia-health-check"],
                capture_output=True, text=True, timeout=5,
            )
            results["bash"] = (
                proc.returncode == 0 and proc.stdout.strip() == "osia-health-check"
            )
    except Exception:
        results["bash"] = False

    # file read --------------------------------------------------------------
    try:
        with open(os.path.abspath(__file__), "r",
                  encoding="utf-8", errors="replace") as f:
            f.read(64)
        results["file_read"] = True
    except Exception:
        results["file_read"] = False

    # browser ----------------------------------------------------------------
    # Importing osia.tools.browser also imports playwright; if either the
    # import or the binary detection fails, browser tools are unusable.
    try:
        from osia.tools.browser_controller import isolated_browser_available
        results["browser"] = isolated_browser_available()
    except Exception:
        results["browser"] = False

    # Log the outcome ---------------------------------------------------------
    for category, ok in results.items():
        status = "✓ available" if ok else "✗ UNAVAILABLE"
        print(f"[OSIA Tools] {category}: {status}")
    unavailable = [k for k, ok in results.items() if not ok]
    summary = f"{len(results) - len(unavailable)}/{len(results)} tool categories available"
    if unavailable:
        summary += f" (unavailable: {', '.join(unavailable)})"
    print(f"[OSIA Tools] health check: {summary}")
    return results
