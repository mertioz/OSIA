#!/usr/bin/env python3
"""
OSIA Browser Tools — Playwright-based web automation.
Provides: navigate, click, fill, read_page
"""

from playwright.sync_api import sync_playwright
import threading
import time

# Global browser state
_browser = None
_context = None
_page = None
_lock = threading.Lock()


def _ensure_browser():
    """Lazily initialize Playwright browser (headless Chromium)."""
    global _browser, _context, _page
    if _browser is None:
        pw = sync_playwright().start()
        _browser = pw.chromium.launch(headless=True)
        _context = _browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        )
        _page = _context.new_page()
    return _page


def navigate(url: str) -> str:
    """Navigate to a URL. Returns page title and URL."""
    with _lock:
        try:
            page = _ensure_browser()
            # Add https:// if no protocol
            if not url.startswith("http"):
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            title = page.title()
            current_url = page.url
            return f"Navigated to: {current_url}\nPage title: {title}"
        except Exception as e:
            return f"ERROR navigating to {url}: {e}"


def click(selector: str) -> str:
    """Click an element matching a CSS selector. Returns confirmation or error."""
    with _lock:
        try:
            page = _ensure_browser()
            page.click(selector, timeout=5000)
            return f"Clicked element: {selector}"
        except Exception as e:
            return f"ERROR clicking {selector}: {e}"


def fill(selector: str, value: str) -> str:
    """Fill an input element matching a CSS selector with a value."""
    with _lock:
        try:
            page = _ensure_browser()
            page.fill(selector, value, timeout=5000)
            return f"Filled {selector} with: {value}"
        except Exception as e:
            return f"ERROR filling {selector}: {e}"


def read_page() -> str:
    """Return the visible text content of the current page (truncated to 4000 chars)."""
    with _lock:
        try:
            page = _ensure_browser()
            # Get all visible text
            text = page.evaluate("""() => {
                return document.body ? document.body.innerText : '';
            }""")
            if not text:
                text = "(empty page)"
            if len(text) > 4000:
                text = text[:4000] + "\n... [TRUNCATED]"
            return f"Page content:\n{text}"
        except Exception as e:
            return f"ERROR reading page: {e}"


def close_browser():
    """Close the browser and clean up resources."""
    global _browser, _context, _page
    with _lock:
        try:
            if _context:
                _context.close()
            if _browser:
                _browser.close()
        except Exception:
            pass
        finally:
            _browser = None
            _context = None
            _page = None
