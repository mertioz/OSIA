"""Expose the installed Playwright browsers through normal Linux PATH lookup."""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).resolve(strict=True)
binaries = root / "bin"
binaries.mkdir(exist_ok=True)
with sync_playwright() as playwright:
    for browser in (playwright.chromium, playwright.firefox):
        executable = Path(browser.executable_path).resolve(strict=True)
        if root not in executable.parents or not os.access(executable, os.X_OK):
            raise SystemExit(f"Invalid packaged browser: {browser.name}")
        (binaries / browser.name).symlink_to(os.path.relpath(executable, binaries))
