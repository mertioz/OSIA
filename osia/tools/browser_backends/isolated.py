"""Adapter for the byte-for-byte preserved isolated browser implementation."""

from __future__ import annotations

from typing import Any, Mapping

from osia.tools.browser_backend import BrowserBackend


_OPERATIONS = frozenset({
    "navigate", "click", "fill", "read_page", "get_current_url", "scroll",
    "go_back", "go_forward", "open_new_tab", "find_and_click", "dom_click",
    "wait_for_load", "get_page_text", "scroll_down", "type_text", "press_key",
    "open_browser", "inspect_form_fields", "fill_form_field", "get_web_state",
    "get_browser_state", "navigate_browser", "inspect_browser_page",
    "wait_for_browser_condition", "activate_browser_element", "list_browser_tabs",
    "select_browser_tab", "close_browser_tab", "reload_browser_page",
    "browser_back", "browser_forward", "open_new_browser_tab",
    "return_to_browser_opener", "search_web", "open_search_result",
    "paginate_browser", "scroll_infinite", "list_browser_dialogs",
    "handle_browser_dialog", "upload_browser_file", "download_browser_file",
    "prepare_upload_authorization", "prepare_browser_action_authorization",
})


class IsolatedBrowserBackend(BrowserBackend):
    name = "isolated"

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        if operation not in _OPERATIONS:
            return _error("capability_unavailable")
        from osia.tools import browser

        function = getattr(browser, operation, None)
        if not callable(function):
            return _error("capability_unavailable")
        if operation in {
            "prepare_upload_authorization", "prepare_browser_action_authorization",
        }:
            return function(dict(arguments))
        return function(**dict(arguments))

    def status(self) -> dict[str, Any]:
        from osia.tools.browser import _browser_owner_snapshot

        snapshot = _browser_owner_snapshot()
        return {
            "backend": self.name,
            "state": "ready" if snapshot.get("activeUrl") else "browser_stopped",
            "snapshot": snapshot,
        }

    def capabilities(self) -> dict[str, Any]:
        return {"backend": self.name, "operations": sorted(_OPERATIONS)}

    def close(self) -> None:
        from osia.tools.browser import close_browser

        close_browser()

    def revoke(self) -> dict[str, Any]:
        return _error("capability_unavailable")


def _error(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "text": f"ERROR: browser operation failed ({code})",
        "error": {"code": code},
    }


__all__ = ["IsolatedBrowserBackend"]
