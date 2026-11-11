"""Deterministic WOR-164/WOR-166 async-owner and popup lifecycle coverage."""

from __future__ import annotations

import asyncio
import base64
import inspect
import threading
import time
from collections import defaultdict

import psutil
import pytest

from osia.tools import browser


class Control:
    def __init__(self):
        self.owner_thread = None
        self.call_threads = []
        self.wrong_thread_calls = []
        self.start_count = 0
        self.stop_count = 0
        self.connect_count = 0
        self.firefox_launch_count = 0
        self.start_delay = 0.0
        self.fail_start_once = False
        self.block_goto = False
        self.operation_started = threading.Event()
        self.operation_cancelled = threading.Event()
        self.popup_created = []
        self.popup_navigated = []
        self.popup_closed = []
        self.popup_specs = []
        self.operations = []

    def check(self, name):
        ident = threading.get_ident()
        if ident != self.owner_thread:
            self.wrong_thread_calls.append((name, ident))
        assert ident == self.owner_thread
        self.call_threads.append(ident)
        self.operations.append(name)

    def add_popup(self, create_delay, url, navigate_delay=0.05, close_delay=None):
        created = threading.Event()
        navigated = threading.Event()
        closed = threading.Event()
        self.popup_created.append(created)
        self.popup_navigated.append(navigated)
        self.popup_closed.append(closed)
        self.popup_specs.append(
            (create_delay, url, navigate_delay, close_delay, created, navigated, closed)
        )


class FakeEmitter:
    def __init__(self, control):
        self.control = control
        self.handlers = defaultdict(list)

    def on(self, event, handler):
        self.control.check(f"on:{event}")
        self.handlers[event].append(handler)

    def off(self, event, handler):
        self.control.check(f"off:{event}")
        try:
            self.handlers[event].remove(handler)
        except (KeyError, ValueError):
            pass

    def emit(self, event, *args):
        self.control.check(f"emit:{event}")
        for handler in tuple(self.handlers[event]):
            handler(*args)


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, text):
        self.page.control.check("keyboard.type")
        self.page.typed.append(text)

    async def press(self, key):
        self.page.control.check("keyboard.press")
        self.page.pressed.append(key)


class FakeLocator:
    def __init__(self, page, label="Continue"):
        self.page = page
        self.label = label
        self.first = self

    async def count(self):
        self.page.control.check("locator.count")
        return 1

    def nth(self, _index):
        self.page.control.check("locator.nth")
        return self

    async def is_visible(self):
        self.page.control.check("locator.is_visible")
        return True

    async def evaluate(self, _expression):
        self.page.control.check("locator.evaluate")
        if "manual_login_required" in _expression:
            return ""
        if "fileCount" in _expression:
            return {
                "disabled": False, "checked": None, "selected": None,
                "expanded": None, "pressed": None, "hidden": False,
                "fileCount": None,
            }
        return self.label

    async def click(self, timeout=None):
        self.page.control.check("locator.click")
        await self.page.click("locator", timeout=timeout)


class FakePage(FakeEmitter):
    def __init__(self, context, url="about:blank", opener=None):
        super().__init__(context.control)
        self.context = context
        self._url = url
        self.opener = opener
        self.closed = False
        self.keyboard = FakeKeyboard(self)
        self.typed = []
        self.pressed = []
        self.content_revision = 0
        self.scroll_position = 0
        self.visibility = "visible"

    @property
    def url(self):
        self.control.check("page.url")
        return self._url

    def is_closed(self):
        self.control.check("page.is_closed")
        return self.closed

    async def goto(self, url, **_kwargs):
        self.control.check("page.goto")
        if self.control.block_goto:
            self.control.operation_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.control.operation_cancelled.set()
                raise
        self._url = url
        self.emit("framenavigated", object())
        return object()

    async def wait_for_load_state(self, _state, timeout=None):
        self.control.check("page.wait_for_load_state")

    async def wait_for_timeout(self, milliseconds):
        self.control.check("page.wait_for_timeout")
        await asyncio.sleep(milliseconds / 1000)

    async def bring_to_front(self):
        self.control.check("page.bring_to_front")

    async def screenshot(self, **_kwargs):
        self.control.check("page.screenshot")
        return f"shot:{self._url}".encode()

    async def click(self, _selector, timeout=None):
        self.control.check("page.click")
        self.content_revision += 1

    async def fill(self, _selector, _value, timeout=None):
        self.control.check("page.fill")

    async def evaluate(self, expression):
        self.control.check("page.evaluate")
        if expression == "document.visibilityState":
            return self.visibility
        if "return window.scrollY - before;" in expression:
            before = self.scroll_position
            delta = int(expression.split("top: ", 1)[1].split(",", 1)[0])
            self.scroll_position = max(0, min(1000, before + delta))
            return self.scroll_position - before
        if expression == browser._CONTENT_FINGERPRINT_JS:
            return f"content-revision:{self.content_revision}"
        if "innerText" in expression:
            return f"content:{self._url}"
        if expression == "location.href":
            return self._url
        return None

    def locator(self, selector):
        self.control.check("page.locator")
        return FakeLocator(self, selector)

    def get_by_text(self, text, exact=False):
        self.control.check("page.get_by_text")
        return FakeLocator(self, str(text))

    def get_by_role(self, _role, name=None):
        self.control.check("page.get_by_role")
        return FakeLocator(self, str(name or "Continue"))

    async def query_selector(self, selector):
        self.control.check("page.query_selector")
        return object() if selector == "#present" else None

    async def go_back(self, timeout=None):
        self.control.check("page.go_back")
        self._url = "https://previous.test"
        self.emit("framenavigated", object())
        return object()

    async def go_forward(self, timeout=None):
        self.control.check("page.go_forward")
        self._url = "https://forward.test"
        self.emit("framenavigated", object())
        return object()

    def manual_close(self):
        self.control.check("page.manual_close")
        if self.closed:
            return
        self.closed = True
        if self in self.context._pages:
            self.context._pages.remove(self)
        self.emit("close")

    def crash(self):
        self.control.check("page.crash")
        self.closed = True
        if self in self.context._pages:
            self.context._pages.remove(self)
        self.emit("crash")


class FakeContext(FakeEmitter):
    def __init__(self, control, initial_url="https://opener.test"):
        super().__init__(control)
        self._pages = [FakePage(self, initial_url)]
        self.closed = False
        self.browser = None

    @property
    def pages(self):
        self.control.check("context.pages")
        if self.closed:
            raise RuntimeError("context closed")
        return list(self._pages)

    async def new_page(self):
        self.control.check("context.new_page")
        page = FakePage(self)
        self._pages.append(page)
        self.emit("page", page)
        return page

    async def close(self):
        self.control.check("context.close")
        if self.closed:
            return
        self.closed = True
        self.emit("close")

    def schedule_popups(self):
        loop = asyncio.get_running_loop()
        opener = self._pages[0]
        for spec in self.control.popup_specs:
            create_delay, url, nav_delay, close_delay, created, navigated, closed = spec

            def create(
                url=url,
                nav_delay=nav_delay,
                close_delay=close_delay,
                created=created,
                navigated=navigated,
                closed=closed,
            ):
                self.control.check("manual_popup.create")
                popup = FakePage(self, "about:blank", opener=opener)
                self._pages.append(popup)
                self.emit("page", popup)
                opener.emit("popup", popup)
                created.set()

                async def navigate_later():
                    await asyncio.sleep(nav_delay)
                    await popup.goto(url)
                    navigated.set()
                    if close_delay is not None:
                        await asyncio.sleep(close_delay)
                        popup.manual_close()
                        closed.set()

                asyncio.create_task(navigate_later())

            loop.call_later(create_delay, create)


class FakeBrowser(FakeEmitter):
    def __init__(self, control, context):
        super().__init__(control)
        self.contexts = [context]
        self.connected = True
        context.browser = self

    def is_connected(self):
        self.control.check("browser.is_connected")
        return self.connected

    async def new_context(self):
        self.control.check("browser.new_context")
        context = FakeContext(self.control)
        context.browser = self
        self.contexts.append(context)
        return context

    async def close(self):
        self.control.check("browser.close")
        if self.connected:
            self.connected = False
            self.emit("disconnected")

    def disconnect_external(self):
        self.control.check("browser.disconnect_external")
        self.connected = False
        self.emit("disconnected")


class FakeChromium:
    def __init__(self, control):
        self.control = control

    async def connect_over_cdp(self, _url):
        self.control.check("chromium.connect_over_cdp")
        self.control.connect_count += 1
        context = FakeContext(
            self.control, f"https://opener-{self.control.connect_count}.test"
        )
        result = FakeBrowser(self.control, context)
        context.schedule_popups()
        return result


class FakeFirefox:
    def __init__(self, control):
        self.control = control

    async def launch_persistent_context(self, *_args, **_kwargs):
        self.control.check("firefox.launch_persistent_context")
        self.control.firefox_launch_count += 1
        context = FakeContext(self.control, "https://firefox.test")
        context.browser = FakeBrowser(self.control, context)
        context.schedule_popups()
        return context


class FakeDriver:
    def __init__(self, control):
        self.control = control
        self.chromium = FakeChromium(control)
        self.firefox = FakeFirefox(control)

    async def stop(self):
        self.control.check("playwright.stop")
        self.control.stop_count += 1


class FakeManager:
    def __init__(self, control):
        self.control = control

    async def start(self):
        self.control.owner_thread = threading.get_ident()
        self.control.start_count += 1
        if self.control.start_delay:
            await asyncio.sleep(self.control.start_delay)
        if self.control.fail_start_once:
            self.control.fail_start_once = False
            raise RuntimeError("planned startup failure")
        return FakeDriver(self.control)


@pytest.fixture
def owner_harness(monkeypatch):
    control = Control()
    owner = browser._AsyncBrowserOwner()
    monkeypatch.setattr(browser, "_owner", owner)
    monkeypatch.setattr(browser, "async_playwright", lambda: FakeManager(control))
    monkeypatch.setattr(browser, "_is_cdp_alive", lambda: True)
    monkeypatch.setattr(browser, "_detect_chromium_browser", lambda: "fake-brave")
    monkeypatch.setattr(browser, "_detect_firefox", lambda: "fake-firefox")
    monkeypatch.setattr(browser, "_processes_using_profile", lambda _path: [])
    monkeypatch.setattr(browser, "_raise_browser_window", lambda _kind=None: None)
    monkeypatch.setattr(
        browser, "_foreground_is_osia_browser", lambda _kind=None: True
    )
    yield control, owner
    try:
        owner.close()
    except Exception:
        pass


def test_owner_starts_once_and_concurrent_callers_share_startup(owner_harness):
    control, owner = owner_harness
    control.start_delay = 0.05
    barrier = threading.Barrier(9)
    results = []

    def worker():
        barrier.wait()
        results.append(browser.get_current_url())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 8
    assert all(value == "Current URL: https://opener-1.test" for value in results)
    assert control.start_count == 1
    assert control.connect_count == 1
    assert set(control.call_threads) == {owner.snapshot()["threadId"]}
    browser.close_browser()
    assert control.wrong_thread_calls == []


def test_idle_popup_about_blank_then_delayed_navigation_is_authoritative(owner_harness):
    control, owner = owner_harness
    control.add_popup(0.03, "https://popup.test/delayed", navigate_delay=0.15)
    assert browser.get_current_url() == "Current URL: https://opener-1.test"
    assert control.popup_created[0].wait(1)
    assert owner.snapshot()["activeUrl"] == "about:blank"
    assert owner.snapshot()["registryCount"] == 2
    assert control.popup_navigated[0].wait(1)
    assert owner.snapshot()["activeUrl"] == "https://popup.test/delayed"
    assert browser.get_current_url() == "Current URL: https://popup.test/delayed"
    assert browser.read_page() == "Page content:\ncontent:https://popup.test/delayed"


def test_close_fallback_multiple_popups_and_duplicate_events(owner_harness):
    control, owner = owner_harness
    control.add_popup(0.02, "https://first.test", navigate_delay=0.01)
    control.add_popup(
        0.05, "https://second.test", navigate_delay=0.01, close_delay=0.05
    )
    browser.get_current_url()
    assert control.popup_navigated[1].wait(1)
    assert owner.snapshot()["activeUrl"] == "https://second.test"
    assert owner.snapshot()["registryCount"] == 3
    assert control.popup_closed[1].wait(1)
    assert owner.snapshot()["activeUrl"] == "https://opener-1.test"
    assert owner.snapshot()["registryCount"] == 2


def test_browser_disconnect_reconnects_without_stale_pages(owner_harness):
    control, owner = owner_harness
    assert browser.get_current_url() == "Current URL: https://opener-1.test"

    async def disconnect():
        owner._browser.disconnect_external()

    owner._op_test_disconnect = disconnect
    owner.call("test_disconnect")
    assert owner.snapshot()["backend"] is None
    assert browser.get_current_url() == "Current URL: https://opener-2.test"
    assert control.connect_count == 2
    assert owner.snapshot()["registryCount"] == 1


def test_crash_recovers_and_stale_context_event_cannot_restore_page(owner_harness):
    _control, owner = owner_harness
    assert browser.get_current_url() == "Current URL: https://opener-1.test"

    async def crash():
        owner._active_page.crash()

    owner._op_test_crash = crash
    owner.call("test_crash")
    assert owner.snapshot()["registryCount"] == 0
    assert browser.get_current_url() == "Current URL: about:blank"
    assert owner.snapshot()["registryCount"] == 1

    async def disconnect_and_emit_stale():
        old_context = owner._context
        owner._browser.disconnect_external()
        old_context.emit("page", FakePage(old_context, "https://stale.test"))

    owner._op_test_disconnect_and_stale = disconnect_and_emit_stale
    owner.call("test_disconnect_and_stale")
    assert owner.snapshot()["registryCount"] == 0
    assert owner.snapshot()["activeUrl"] != "https://stale.test"
    assert browser.get_current_url() == "Current URL: https://opener-2.test"


def test_navigation_click_fill_keyboard_tabs_and_state(owner_harness):
    _control, owner = owner_harness
    result = browser.navigate("example.test")
    assert result["text"] == "Navigated to: https://example.test"
    assert "screenshot" not in result
    assert result["receipt"]["outcome"] == "confirmed_success"
    assert browser.click("#button")["receipt"]["outcome"] == "confirmed_success"
    assert browser.find_and_click("Continue")["receipt"]["outcome"] == "confirmed_success"
    assert browser.fill("#field", "value") == "Field filled; the private value was withheld."
    assert browser.type_text("#field", "abc") == "Text entered; the private value was withheld."
    assert browser.press_key("Enter") == "Pressed key: Enter"
    assert browser.scroll("down", 40) == "Scrolled down by 40px"
    assert browser.scroll("up", 600) == "Scrolled up by 40px"
    assert browser.scroll("up", 40) == "The browser page stayed at the same scroll position."
    assert browser.scroll_down() == "Scrolled down one viewport"
    new_tab = browser.open_new_tab("new.test")
    assert new_tab["receipt"]["outcome"] == "confirmed_success"
    assert new_tab["data"]["safe_url"] == "https://new.test"
    assert owner.snapshot()["activeUrl"] == "https://new.test"
    state = browser.get_web_state("new.test", "content:", "#present")
    assert state == {
        "available": True,
        "url": "https://new.test",
        "checks": {
            "url_contains": True,
            "text_contains": True,
            "selector_found": True,
        },
    }


@pytest.mark.parametrize("backend", ["cdp", "bidi"])
@pytest.mark.parametrize("mouse_disabled", [False, True])
def test_page_tools_preserve_background_focus(
    owner_harness, monkeypatch, backend, mouse_disabled,
):
    from osia.slash_commands import chat_command_scope

    control, owner = owner_harness
    monkeypatch.setattr(
        "osia.conversations.conversation_special_mode", lambda _: mouse_disabled,
    )
    raises = []
    monkeypatch.setattr(browser, "_raise_browser_window", raises.append)

    async def capture():
        page = await owner._ensure_browser()
        return await owner._screenshot_b64(page)

    owner._op_test_capture = capture
    with chat_command_scope("background-browser", None):
        if backend == "bidi":
            opened = owner.call("open_firefox_bidi", "https://background.test")
        else:
            opened = owner.call("open_cdp_browser", "fake-brave", "https://background.test")
        assert opened["ok"]
        original = opened["data"]["state"]
        assert browser.click("#button")["ok"]
        assert browser.fill("#field", "value").startswith("Field filled")
        assert browser.type_text("#field", "abc").startswith("Text entered")
        for key in ("Tab", "Shift+Tab", "Enter"):
            assert browser.press_key(key) == f"Pressed key: {key}"
        assert base64.b64decode(owner.call("test_capture")) == b"shot:https://background.test"
        new_tab = browser.open_new_browser_tab("https://second.test", reuse_existing=False)
        assert new_tab["ok"]
        assert browser.select_browser_tab(original)["ok"]
        assert browser.return_to_browser_opener(new_tab["data"]["state"])["ok"]

    assert "page.bring_to_front" not in control.operations
    assert not raises


def test_native_browser_bridge_cannot_use_a_background_tab(owner_harness):
    control, owner = owner_harness
    assert browser.navigate("https://background.test")["ok"]

    async def hide_selected_tab():
        owner._active_page.visibility = "hidden"

    owner._op_test_hide_selected_tab = hide_selected_tab
    owner.call("test_hide_selected_tab")
    previous_clicks = control.operations.count("page.click")
    assert browser.dom_click("Continue") is None
    assert browser.get_web_state()["available"] is False
    assert control.operations.count("page.click") == previous_clicks


def test_exact_tab_reuse_and_intentionally_separate_tab(owner_harness):
    url = "https://projects.test/details?section=work#latest"
    original = browser.navigate_browser(url)["data"]["state"]

    reused = browser.open_new_browser_tab(url)
    assert reused["ok"] is True
    assert reused["data"]["tab_reused"] is True
    assert reused["data"]["state"] == original
    assert len(browser.list_browser_tabs()["data"]["tabs"]) == 1

    separate = browser.open_new_browser_tab(url, reuse_existing=False)
    assert separate["ok"] is True
    assert separate["data"]["tab_reused"] is False
    assert separate["data"]["matching_tab"] == {
        "state": original,
        "safe_url": "https://projects.test/details",
    }
    opened = separate["data"]["state"]
    assert opened["page_id"] != original["page_id"]

    # A refresh of the destination in the new tab keeps its page identity
    # and leaves the previously open tab available for direct selection.
    navigated = browser.navigate_browser(url, opened)["data"]["state"]
    assert navigated["page_id"] == opened["page_id"]
    tabs = browser.list_browser_tabs()["data"]["tabs"]
    assert {tab["state"]["page_id"] for tab in tabs} == {
        original["page_id"], opened["page_id"],
    }
    assert browser.select_browser_tab(original)["data"]["state"] == original


def test_firefox_bidi_reuses_one_persistent_context(owner_harness, monkeypatch):
    control, owner = owner_harness
    monkeypatch.setattr(browser, "resolve_browser_name", lambda _name: "fake-firefox")
    monkeypatch.setattr(browser, "_canonical_browser_name", lambda _path: "firefox")
    first = browser.open_browser("firefox")
    second = browser.open_browser("firefox")
    assert first["text"].startswith("Opened Firefox")
    assert second["text"].startswith("Opened Firefox")
    assert control.firefox_launch_count == 1
    assert owner.snapshot()["backend"] == "bidi"


def test_startup_failure_allows_one_clean_later_restart(owner_harness):
    control, owner = owner_harness
    control.fail_start_once = True
    first = browser.get_current_url()
    assert first.startswith("ERROR reading current URL:")
    second = browser.get_current_url()
    assert second == "Current URL: https://opener-1.test"
    assert control.start_count == 2
    assert owner.snapshot()["state"] == "READY"


def test_shutdown_during_startup_cannot_restart_or_orphan_owner(owner_harness):
    control, owner = owner_harness
    control.start_delay = 0.15
    result = []
    worker = threading.Thread(target=lambda: result.append(browser.get_current_url()))
    worker.start()
    deadline = time.monotonic() + 1
    while control.start_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert control.start_count == 1
    browser.close_browser()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert result and result[0].startswith("ERROR reading current URL:")
    assert owner.snapshot()["state"] == "STOPPED"
    assert not owner.snapshot()["threadAlive"]
    assert control.start_count == 1
    assert control.stop_count == 1


def test_caller_timeout_cancels_and_resets_owner(owner_harness):
    control, owner = owner_harness
    control.block_goto = True
    with pytest.raises(browser._OwnerCallTimeout):
        owner.call("navigate", "https://slow.test", timeout=0.05)
    assert control.operation_started.is_set()
    assert control.operation_cancelled.wait(1)
    assert owner.snapshot()["state"] == "STOPPED"
    assert not owner.snapshot()["threadAlive"]
    assert control.stop_count == 1


def test_shutdown_cancels_active_operation_and_is_idempotent(owner_harness):
    control, owner = owner_harness
    control.block_goto = True
    result = []
    worker = threading.Thread(
        target=lambda: result.append(browser.navigate("https://slow.test"))
    )
    worker.start()
    assert control.operation_started.wait(1)
    browser.close_browser()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert result and result[0].startswith("ERROR: the browser did not open")
    assert control.operation_cancelled.is_set()
    browser.close_browser()
    assert owner.snapshot()["state"] == "STOPPED"
    assert not owner.snapshot()["threadAlive"]
    assert control.stop_count == 1


def test_idle_owner_has_negligible_cpu_and_no_periodic_page_poll(owner_harness):
    _control, owner = owner_harness
    browser.get_current_url()
    native_id = owner.snapshot()["nativeThreadId"]
    process = psutil.Process()

    def cpu_time():
        for item in process.threads():
            if item.id == native_id:
                return item.user_time + item.system_time
        return None

    before = cpu_time()
    time.sleep(0.25)
    after = cpu_time()
    assert before is not None and after is not None
    assert after - before <= 0.02


def test_brave_launch_arguments_and_security_contract_unchanged(monkeypatch, tmp_path):
    captured = {}
    profile = tmp_path / "brave-profile"
    binary = str(tmp_path / "brave.exe")
    monkeypatch.setattr(browser, "OSIA_PROFILE_DIR", str(profile))
    monkeypatch.setattr(browser, "_is_cdp_alive", lambda: False)
    monkeypatch.setattr(browser, "_wait_for_cdp", lambda timeout: True)
    monkeypatch.setattr(browser, "_processes_using_profile", lambda _path: [])
    monkeypatch.setattr(
        browser.subprocess,
        "Popen",
        lambda cmd, **kwargs: captured.setdefault("command", list(cmd)),
    )
    ok, _message = browser._launch_cdp_browser(binary)
    assert ok
    assert captured["command"] == [
        binary,
        f"--user-data-dir={profile}",
        "--remote-debugging-port=9222",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    source = inspect.getsource(browser)
    for forbidden in (
        "--proxy-server",
        "--host-resolver-rules",
        "--user-agent",
        "--disable-web-security",
        "set_offline(",
        ".route(",
        "Login Data",
        "Cookies",
        "wallet",
    ):
        assert forbidden not in source


def test_public_signatures_and_no_module_global_playwright_objects():
    expected = {
        "navigate": "(url: str)",
        "click": "(selector: str)",
        "fill": "(selector: str, value: str) -> str",
        "read_page": "() -> str",
        "get_current_url": "() -> str",
        "scroll": "(direction: str = 'down', amount: int = 600) -> str",
        "go_back": "()",
        "go_forward": "()",
        "open_browser": "(name: str, url: str | None = None)",
        "open_new_tab": "(url: str) -> str",
        "find_and_click": "(text: str)",
        "dom_click": "(description: str)",
        "wait_for_load": "() -> str",
        "get_page_text": "() -> str",
        "scroll_down": "(amount: int | None = None) -> str",
        "type_text": "(selector: str, text: str) -> str",
        "press_key": "(key: str) -> str",
        "close_browser": "()",
    }
    for name, signature in expected.items():
        assert str(inspect.signature(getattr(browser, name), eval_str=False)) == signature
    assert "from playwright.sync_api" not in inspect.getsource(browser)
    assert not hasattr(browser, "_page")
    assert not hasattr(browser, "_context")
    assert not hasattr(browser, "_browser")
