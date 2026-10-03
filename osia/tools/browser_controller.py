"""Stable adapter for OSIA's canonical isolated browser runtime."""

from __future__ import annotations

from typing import Any, Mapping

from osia.tools.browser_backends import IsolatedBrowserBackend


class BrowserController:
    """Forward semantic browser operations to the isolated OSIA runtime."""

    def __init__(self, *, isolated=None) -> None:
        self._isolated = isolated or IsolatedBrowserBackend()
        self._progress_callback = None

    def selected_backend_name(self) -> str:
        return "isolated"

    def selected_backend(self):
        return self._isolated

    def invoke(self, operation: str, arguments: Mapping[str, Any] | None = None) -> Any:
        return self.selected_backend().invoke(operation, dict(arguments or {}))

    def status(self) -> dict[str, Any]:
        return self.selected_backend().status()

    def capabilities(self) -> dict[str, Any]:
        return self.selected_backend().capabilities()

    def close(self) -> None:
        self._isolated.close()

    def revoke(self) -> dict[str, Any]:
        return self.selected_backend().revoke()

    def snapshot(self) -> dict[str, Any]:
        status = self.status()
        snapshot = status.get("snapshot")
        if isinstance(snapshot, dict):
            return snapshot
        targets = status.get("targets") if isinstance(status, dict) else None
        target = targets[0] if isinstance(targets, list) and len(targets) == 1 else {}
        return {
            "backend": self.selected_backend_name(),
            "activeUrl": target.get("safe_url", "") if isinstance(target, dict) else "",
            "browserState": target.get("state_ref") if isinstance(target, dict) else None,
        }

    def set_progress_callback(self, callback) -> None:
        self._progress_callback = callback
        try:
            from osia.tools.browser import set_browser_progress_callback as isolated_callback

            isolated_callback(callback)
        except Exception:
            pass


_CONTROLLER = BrowserController()


def get_browser_controller() -> BrowserController:
    return _CONTROLLER


def set_browser_controller_for_testing(controller: BrowserController | None):
    global _CONTROLLER
    previous = _CONTROLLER
    _CONTROLLER = controller or BrowserController()
    return previous


def isolated_browser_available() -> bool:
    try:
        from osia.tools.browser import _detect_chromium_browser

        return _detect_chromium_browser() is not None
    except Exception:
        return False


def _invoke(operation: str, **arguments):
    return _CONTROLLER.invoke(operation, arguments)


def navigate(url: str): return _invoke("navigate", url=url)
def click(selector: str): return _invoke("click", selector=selector)
def fill(selector: str, value: str): return _invoke("fill", selector=selector, value=value)
def read_page(): return _invoke("read_page")
def get_current_url(): return _invoke("get_current_url")
def scroll(direction: str = "down", amount: int = 600): return _invoke("scroll", direction=direction, amount=amount)
def go_back(): return _invoke("go_back")
def go_forward(): return _invoke("go_forward")
def open_new_tab(url: str): return _invoke("open_new_tab", url=url)
def find_and_click(text: str): return _invoke("find_and_click", text=text)
def dom_click(description: str): return _invoke("dom_click", description=description)
def wait_for_load(): return _invoke("wait_for_load")
def get_page_text(): return _invoke("get_page_text")
def scroll_down(amount: int | None = None): return _invoke("scroll_down", amount=amount)
def type_text(selector: str, text: str): return _invoke("type_text", selector=selector, text=text)
def press_key(key: str): return _invoke("press_key", key=key)
def open_browser(name: str, url: str | None = None): return _invoke("open_browser", name=name, url=url)
def inspect_form_fields(include_hidden=False, include_disabled=False, max_fields=50): return _invoke("inspect_form_fields", include_hidden=include_hidden, include_disabled=include_disabled, max_fields=max_fields)
def fill_form_field(field_ref: str, value): return _invoke("fill_form_field", field_ref=field_ref, value=value)
def get_web_state(expect_url=None, expect_text=None, expect_selector=None): return _invoke("get_web_state", expect_url=expect_url, expect_text=expect_text, expect_selector=expect_selector)
def get_browser_state(): return _invoke("get_browser_state")
def navigate_browser(url: str, state_ref=None, wait_spec=None): return _invoke("navigate_browser", url=url, state_ref=state_ref, wait_spec=wait_spec)
def inspect_browser_page(state_ref=None, mode="snapshot", target_ref=None, cursor=None, frame_ref=None, max_items=40): return _invoke("inspect_browser_page", state_ref=state_ref, mode=mode, target_ref=target_ref, cursor=cursor, frame_ref=frame_ref, max_items=max_items)
def wait_for_browser_condition(state_ref: dict, wait_spec: dict): return _invoke("wait_for_browser_condition", state_ref=state_ref, wait_spec=wait_spec)
def activate_browser_element(state_ref: dict, element_ref: str, wait_spec=None): return _invoke("activate_browser_element", state_ref=state_ref, element_ref=element_ref, wait_spec=wait_spec)
def list_browser_tabs(state_ref=None): return _invoke("list_browser_tabs", state_ref=state_ref)
def select_browser_tab(state_ref: dict): return _invoke("select_browser_tab", state_ref=state_ref)
def close_browser_tab(state_ref: dict): return _invoke("close_browser_tab", state_ref=state_ref)
def reload_browser_page(state_ref: dict): return _invoke("reload_browser_page", state_ref=state_ref)
def browser_back(state_ref: dict): return _invoke("browser_back", state_ref=state_ref)
def browser_forward(state_ref: dict): return _invoke("browser_forward", state_ref=state_ref)
def open_new_browser_tab(url: str, state_ref=None): return _invoke("open_new_browser_tab", url=url, state_ref=state_ref)
def return_to_browser_opener(state_ref: dict): return _invoke("return_to_browser_opener", state_ref=state_ref)
def search_web(query: str, state_ref=None): return _invoke("search_web", query=query, state_ref=state_ref)
def open_search_result(state_ref: dict, result_ref: str): return _invoke("open_search_result", state_ref=state_ref, result_ref=result_ref)
def paginate_browser(state_ref: dict, next_ref: str, max_pages=1): return _invoke("paginate_browser", state_ref=state_ref, next_ref=next_ref, max_pages=max_pages)
def scroll_infinite(state_ref: dict, max_steps=8): return _invoke("scroll_infinite", state_ref=state_ref, max_steps=max_steps)
def list_browser_dialogs(state_ref: dict): return _invoke("list_browser_dialogs", state_ref=state_ref)
def handle_browser_dialog(state_ref: dict, dialog_ref: str, action: str, prompt_text=None): return _invoke("handle_browser_dialog", state_ref=state_ref, dialog_ref=dialog_ref, action=action, prompt_text=prompt_text)
def upload_browser_file(state_ref: dict, element_ref: str, artifact_id=None, local_path=None, prepared_upload_ref=None, destination_origin=None, file_size=None, detected_mime=None, operation_expires_at=None): return _invoke("upload_browser_file", state_ref=state_ref, element_ref=element_ref, artifact_id=artifact_id, local_path=local_path, prepared_upload_ref=prepared_upload_ref, destination_origin=destination_origin, file_size=file_size, detected_mime=detected_mime, operation_expires_at=operation_expires_at)
def download_browser_file(state_ref: dict, element_ref: str): return _invoke("download_browser_file", state_ref=state_ref, element_ref=element_ref)
def prepare_upload_authorization(arguments: dict): return _CONTROLLER.invoke("prepare_upload_authorization", arguments)
def prepare_browser_action_authorization(arguments: dict): return _CONTROLLER.invoke("prepare_browser_action_authorization", arguments)
def _browser_owner_snapshot(): return _CONTROLLER.snapshot()
def set_browser_progress_callback(callback): return _CONTROLLER.set_progress_callback(callback)
def close_browser(): return _CONTROLLER.close()


__all__ = [name for name in globals() if not name.startswith("_")] + ["_browser_owner_snapshot"]
