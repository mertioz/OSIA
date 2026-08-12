"""Personal Browser adapter over the same-user local broker."""

from __future__ import annotations

from typing import Any, Mapping

from osia.tools.browser_backend import BrowserBackend


PERSONAL_OPERATIONS = frozenset({
    "get_browser_state", "inspect_browser_page", "inspect_form_fields",
    "navigate_browser", "list_browser_tabs", "select_browser_tab",
    "close_browser_tab", "reload_browser_page", "browser_back",
    "browser_forward", "return_to_browser_opener", "activate_browser_element",
    "fill_form_field", "wait_for_browser_condition", "list_browser_dialogs",
    "handle_browser_dialog", "prepare_browser_action_authorization",
})


class PersonalBrowserBackend(BrowserBackend):
    name = "personal"

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        if operation not in PERSONAL_OPERATIONS:
            return _error("capability_unavailable")
        try:
            from osia.personal_browser.broker import get_personal_browser_broker

            return get_personal_browser_broker().invoke(operation, dict(arguments))
        except Exception:
            return _error("disconnected")

    def status(self) -> dict[str, Any]:
        try:
            from osia.personal_browser.broker import get_personal_browser_broker

            return get_personal_browser_broker().status()
        except Exception:
            return {"backend": self.name, "state": "disconnected", "targets": []}

    def capabilities(self) -> dict[str, Any]:
        status = self.status()
        remote = status.get("capabilities")
        operations = sorted(PERSONAL_OPERATIONS)
        if isinstance(remote, list):
            operations = sorted(set(operations).intersection(map(str, remote)))
        return {"backend": self.name, "operations": operations}

    def close(self) -> None:
        try:
            from osia.personal_browser.broker import get_personal_browser_broker

            get_personal_browser_broker().close()
        except Exception:
            pass

    def revoke(self) -> dict[str, Any]:
        try:
            from osia.personal_browser.broker import get_personal_browser_broker

            return get_personal_browser_broker().invoke("revoke", {})
        except Exception:
            return _error("disconnected")


def _error(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "text": f"ERROR: personal browser operation failed ({code})",
        "error": {"code": code},
    }


__all__ = ["PERSONAL_OPERATIONS", "PersonalBrowserBackend"]
