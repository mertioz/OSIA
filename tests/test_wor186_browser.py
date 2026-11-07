from __future__ import annotations

import asyncio
import hashlib
import json
import importlib.util
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import tool_contracts
from osia import action_policy
from osia.tools import browser
from osia.mcp_client import registry as mcp_registry
from osia.tools.browser_artifacts import (
    BrowserArtifactError,
    BrowserArtifactStore,
    MAX_DOWNLOAD_BYTES,
    MAX_SESSION_TEMP_BYTES,
    MAX_UPLOAD_BYTES,
    detect_content,
    revalidate_upload,
    set_browser_artifact_store_for_testing,
    validate_upload_path,
)
from osia.tools.browser_state import (
    BoundReferenceStore,
    BrowserReferenceError,
    BrowserStateTracker,
    DispatchState,
    ReceiptOutcome,
    WaitSpec,
    action_result,
    make_receipt,
    safe_url_display,
)
from wor186_web_fixture import DOWNLOAD_BYTES, PRIVATE_SENTINEL, Wor186WebFixture


NEW_BROWSER_TOOLS = (
    "get_browser_state", "navigate_browser", "inspect_browser_page",
    "wait_for_browser_condition", "activate_browser_element",
    "list_browser_tabs", "select_browser_tab", "close_browser_tab",
    "reload_browser_page", "browser_back", "browser_forward",
    "open_new_browser_tab", "return_to_browser_opener", "search_web",
    "open_search_result", "paginate_browser", "scroll_infinite",
    "list_browser_dialogs", "handle_browser_dialog",
    "upload_browser_file", "download_browser_file",
)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _success(result):
    assert isinstance(result, dict), result
    assert result.get("ok") is True, result
    receipt = result["receipt"]
    assert receipt["outcome"] == "confirmed_success"
    assert receipt["dispatch_state"] == "effect_confirmed"
    return result.get("data") or {}


def _navigate(url, state=None):
    return _success(browser.navigate_browser(url, state))["state"]


@pytest.fixture(scope="module")
def controlled_browser(tmp_path_factory):
    binary = browser.resolve_browser_name("brave")
    if not binary:
        pytest.skip("a compatible installed Brave browser is unavailable")
    original = {
        "owner": browser._owner,
        "port": browser.CDP_PORT,
        "url": browser.CDP_URL,
        "profile": browser.OSIA_PROFILE_DIR,
        "detect": browser._detect_chromium_browser,
        "raise": browser._raise_browser_window,
    }
    owner = browser._AsyncBrowserOwner()
    profile_root = tmp_path_factory.mktemp("wor186-browser")
    profile = profile_root / "profile"
    artifact_root = profile_root / "artifacts"
    previous_store = set_browser_artifact_store_for_testing(
        BrowserArtifactStore(artifact_root)
    )
    port = _free_port()
    browser._owner = owner
    browser.CDP_PORT = port
    browser.CDP_URL = f"http://127.0.0.1:{port}"
    browser.OSIA_PROFILE_DIR = str(profile)
    browser._detect_chromium_browser = lambda: binary
    browser._raise_browser_window = lambda _kind=None: None
    with Wor186WebFixture() as web:
        previous_search = browser._set_search_template_for_testing(
            web.base + "/search?q={query}"
        )
        try:
            yield SimpleNamespace(
                web=web, owner=owner, profile=profile,
                artifact_root=artifact_root,
            )
        finally:
            browser._set_search_template_for_testing(previous_search)
            browser.close_browser()
            leftovers = browser._processes_using_profile(str(profile))
            if leftovers:
                browser._terminate_processes(leftovers)
            set_browser_artifact_store_for_testing(previous_store)
            browser._owner = original["owner"]
            browser.CDP_PORT = original["port"]
            browser.CDP_URL = original["url"]
            browser.OSIA_PROFILE_DIR = original["profile"]
            browser._detect_chromium_browser = original["detect"]
            browser._raise_browser_window = original["raise"]
            shutil.rmtree(profile_root, ignore_errors=True)
    assert not browser._processes_using_profile(str(profile))
    assert not profile_root.exists()


def test_browser_state_identity_lifecycle_and_opaque_bound_references():
    tracker = BrowserStateTracker()
    context_one, context_two = object(), object()
    page, popup = object(), object()
    tracker.start_context(context_one)
    first = tracker.register_page(page)
    assert re.search(r"\b0x[0-9a-fA-F]{12,}\b", json.dumps(first.to_dict())) is None
    navigated = tracker.note_navigation(page)
    assert navigated.page_id == first.page_id
    assert navigated.navigation_id != first.navigation_id
    assert navigated.document_epoch == first.document_epoch + 1
    with pytest.raises(BrowserReferenceError):
        tracker.validate(first.to_dict())
    assert tracker.validate(navigated.to_dict()) is page
    popup_ref = tracker.register_page(popup, opener=page, popup=True)
    assert tracker.opener(popup) is page
    tracker.retire_page(popup)
    with pytest.raises(BrowserReferenceError):
        tracker.validate(popup_ref.to_dict())
    tracker.start_context(context_two)
    with pytest.raises(BrowserReferenceError):
        tracker.validate(navigated.to_dict())
    sequences = [event["sequence"] for event in tracker.events()]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))

    refs = BoundReferenceStore(clock=lambda: 10.0)
    current = tracker.register_page(object())
    element_ref = refs.issue("button", current, object())
    assert element_ref.startswith("el_")
    first_payload = object()
    second_payload = object()
    stable_ref = refs.issue(
        "link", current, first_payload, binding_key="same-rendered-target",
    )
    assert refs.issue(
        "link", current, second_payload, binding_key="same-rendered-target",
    ) == stable_ref
    assert refs.resolve(stable_ref, current, kinds={"link"}) is second_payload
    assert refs.issue(
        "link", current, object(), binding_key="different-rendered-target",
    ) != stable_ref
    changed_document = tracker.note_navigation(tracker.validate(current))
    assert refs.issue(
        "link", changed_document, object(), binding_key="same-rendered-target",
    ) != stable_ref
    foreign = dict(current.to_dict())
    foreign["navigation_id"] = first.navigation_id
    with pytest.raises(BrowserReferenceError):
        refs.resolve(element_ref, type(current).parse(foreign))


def test_wait_spec_is_bounded_and_application_owned():
    parsed = WaitSpec.parse({
        "condition_type": "dom_selector_state",
        "expected": {"primitive": "main", "state": "visible"},
        "timeout_seconds": 2,
        "quiet_period_seconds": 0.1,
    })
    assert parsed.expected == {"primitive": "main", "state": "visible"}
    with pytest.raises(ValueError):
        WaitSpec.parse({
            "condition_type": "dom_selector_state",
            "expected": {"primitive": "script[src*=private]", "state": "visible"},
            "timeout_seconds": 2,
            "quiet_period_seconds": 0.1,
        })
    with pytest.raises(ValueError):
        WaitSpec.parse({
            "condition_type": "custom_postcondition",
            "expected": [{
                "condition_type": "stable_content_fingerprint",
                "timeout_seconds": 3,
                "quiet_period_seconds": 0.1,
            }],
            "timeout_seconds": 1,
            "quiet_period_seconds": 0.1,
        })


def test_all_browser_tools_have_complete_central_contracts():
    for name in NEW_BROWSER_TOOLS:
        spec = tool_contracts.get_tool_spec(name)
        assert spec is not None, name
        assert spec.input_schema.get("type") == "object", name
        assert spec.permission_category != "", name
        assert spec.timeout_seconds > 0, name
        assert spec.redaction_policy, name
        # WOR-301: the mode-eligibility vocabulary is gone entirely.
        assert not hasattr(spec, "chat_eligible"), name
        assert not hasattr(spec, "scheduled_eligible"), name
        assert spec.retry.value in {
            "not_started_only", "never_after_dispatch",
            "read_once_after_confirmed_failure",
        }




def test_engine_surfaces_never_reduce_the_model_visible_inventory():
    from osia import agent as agent_module

    with tool_contracts.effective_tool_surface_scope(""):
        chat_names = {
            spec.name for spec in tool_contracts.effective_static_tool_specs()
        }
        prompt = agent_module.get_system_prompt(
            "en", model="deepseek-v4-pro", provider="deepseek",
        )

    # WOR-300/WOR-303: there is one canonical Chat surface; it gates dispatch,
    # never the visible inventory.
    assert all(spec.input_schema.get("type") == "object"
               for spec in tool_contracts.effective_static_tool_specs())
    prompt_names = set(re.findall(r"^- ([a-z0-9_]+)\(", prompt, re.MULTILINE))
    assert prompt_names == chat_names
    assert {"open_uri", "navigate", "bash", "navigate_browser"} <= prompt_names

    with tool_contracts.effective_tool_surface_scope("scheduled"):
        scheduled_specs = tool_contracts.effective_static_tool_specs()
        scheduled_prompt = agent_module.get_system_prompt(
            "en", model="deepseek-v4-pro", provider="deepseek",
        )
    assert {spec.name for spec in scheduled_specs} == chat_names
    assert set(re.findall(
        r"^- ([a-z0-9_]+)\(", scheduled_prompt, re.MULTILINE,
    )) == chat_names


















def test_artifact_limits_identity_classification_and_privacy(tmp_path):
    source = tmp_path / "benign.txt"
    source.write_bytes(DOWNLOAD_BYTES)
    validated = validate_upload_path(source.resolve())
    assert validated.size == len(DOWNLOAD_BYTES)
    assert revalidate_upload(validated) == validated
    source.write_bytes(DOWNLOAD_BYTES + b"changed")
    with pytest.raises(BrowserArtifactError, match="file_changed"):
        revalidate_upload(validated)
    oversized_upload = tmp_path / "too-large-upload.bin"
    with oversized_upload.open("wb") as handle:
        handle.truncate(MAX_UPLOAD_BYTES + 1)
    with pytest.raises(BrowserArtifactError, match="file_size_limit_exceeded"):
        validate_upload_path(oversized_upload.resolve())
    store = BrowserArtifactStore(tmp_path / "private")
    executable = tmp_path / "payload.exe"
    executable.write_bytes(b"MZ" + b"\0" * 32)
    active = store.finalize_download(executable, suggested_filename="..\\private.exe")
    assert active["classification"] == "active_content"
    assert active["executable"] is False
    archive = tmp_path / "payload.zip"
    archive.write_bytes(b"PK\x03\x04" + b"\0" * 32)
    archived = store.finalize_download(archive, suggested_filename="private.zip")
    assert archived["classification"] == "archive_content"
    assert archived["extractable"] is False
    assert "private_filename" not in active
    oversized_download = tmp_path / "too-large-download.bin"
    with oversized_download.open("wb") as handle:
        handle.truncate(MAX_DOWNLOAD_BYTES + 1)
    with pytest.raises(BrowserArtifactError, match="file_size_limit_exceeded"):
        store.finalize_download(oversized_download, suggested_filename="large.bin")
    assert detect_content(b"#!/bin/sh\n", "safe.txt")[1] == "active_content"
    if hasattr(os, "symlink"):
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(source)
        except OSError:
            pass
        else:
            with pytest.raises(BrowserArtifactError, match="indirect_path_rejected"):
                validate_upload_path(link.absolute())


def test_safe_url_display_never_exposes_query_fragment_userinfo_or_file_path():
    raw = "https://account:secret@example.test/private/path?token=QUERY#fragment"
    assert safe_url_display(raw) == "https://example.test/private/path"
    assert safe_url_display("file:///C:/private/secret.txt") == "file:///[private]"
    assert safe_url_display("about:blank?token=QUERY#fragment") == "about:blank"
    assert safe_url_display("https://[2001:db8::1]:8443/path?q=ignored") == "https://[2001:db8::1]:8443/path"
    assert safe_url_display("https://example.test:invalid/path") == "invalid-url"


@pytest.mark.parametrize(
    ("platform_name", "candidate", "available_path"),
    (
        ("win32", r"C:\Program Files\Browser\browser.exe", r"C:\Program Files\Browser\browser.exe"),
        ("linux", "chromium", "/usr/bin/chromium"),
        ("darwin", "/Applications/Browser.app/Contents/MacOS/Browser", "/Applications/Browser.app/Contents/MacOS/Browser"),
    ),
)
def test_browser_discovery_is_deterministic_on_supported_platforms(
    monkeypatch, platform_name, candidate, available_path,
):
    monkeypatch.setattr(browser.sys, "platform", platform_name)
    monkeypatch.setattr(
        browser.os.path, "exists", lambda value: str(value) == available_path,
    )
    monkeypatch.setattr(
        browser.os.path, "join", lambda directory, name: f"{directory}/{name}",
    )
    monkeypatch.setattr(browser.shutil, "which", lambda _value: None)
    assert browser._find_binary(candidate) == available_path
    assert any(
        ".app/Contents/MacOS/" in path for path in browser._MACOS_NAME_PATHS["brave"]
    )


def test_browser_unavailable_is_typed_and_never_downloads_a_runtime(monkeypatch):
    monkeypatch.setattr(browser, "resolve_browser_name", lambda _name: None)
    unavailable = browser.open_browser("missing-browser")
    assert unavailable["ok"] is False
    assert unavailable["error"]["code"] == "browser_unavailable"
    assert unavailable["receipt"]["dispatch_state"] == "not_dispatched"
    assert "supported_installation_options" in unavailable["data"]


def test_legacy_browser_errors_withhold_selectors_values_urls_and_exceptions(monkeypatch):
    sentinel = "WOR186_PRIVATE_SELECTOR_VALUE"

    def fail(*_args, **_kwargs):
        raise RuntimeError(
            f"{sentinel} https://example.test/path?token={sentinel}#fragment"
        )

    monkeypatch.setattr(browser._owner, "call", fail)
    results = (
        browser.click(f"#{sentinel}"),
        browser.fill(f"#{sentinel}", sentinel),
        browser.type_text(f"#{sentinel}", sentinel),
        browser.find_and_click(sentinel),
        browser.open_new_tab(
            f"https://example.test/path?token={sentinel}#fragment"
        ),
    )
    serialized = json.dumps(results)
    assert sentinel not in serialized
    assert "?" not in serialized
    assert "#fragment" not in serialized








def test_agent_model_feedback_includes_typed_browser_data_only_inside_turn():
    from osia import agent as agent_module

    tracker = BrowserStateTracker()
    tracker.start_context(object())
    page = object()
    before = tracker.register_page(page)
    after = tracker.note_navigation(page)
    receipt = make_receipt(
        before_state=before,
        after_state=after,
        dispatch_state=DispatchState.EFFECT_CONFIRMED,
        outcome=ReceiptOutcome.CONFIRMED_SUCCESS,
        evidence=[{
            "structured_snapshot": True,
            "safe_url_after": "https://fixture.invalid/path?private=yes#fragment",
        }],
    )
    result = action_result(
        ok=True,
        receipt=receipt,
        data={
            "state": after.to_dict(),
            "headings": [{"text": "Controlled heading"}],
            "links": [{"element_ref": "el_safe_reference"}],
            "artifact": {
                "artifact_id": "web_" + "a" * 32,
                "size": 12,
                "mime_type": "text/plain",
                "classification": "opaque_content",
                "local_path": "C:/private/private-name.txt",
                "sha256": "private-internal-hash",
            },
            "local_path": "C:/private/private-name.txt",
            "prompt_text": "private form value",
        },
    )

    # The historic helper remains status-only for logging/error contracts.
    assert agent_module._result_text(result) == (
        "Browser operation completed with verified evidence."
    )
    model_projection = json.loads(
        agent_module._model_result_text(result, "paginate_browser")
    )
    assert model_projection["ok"] is True
    assert model_projection["typed_outcome"] == "confirmed_success"
    assert model_projection["dispatch_state"] == "effect_confirmed"
    assert model_projection["current_page_ref"] == after.to_dict()
    assert model_projection["state_changed"] == {
        "navigation": True, "document": True,
    }
    assert model_projection["operation_data"]["headings"][0]["text"] == (
        "Controlled heading"
    )
    assert model_projection["operation_data"]["links"][0]["element_ref"] == (
        "el_safe_reference"
    )
    assert model_projection["next_valid_operations"] == [
        "inspect_browser_page"
    ]
    serialized = json.dumps(model_projection)
    for private in (
        "private-name.txt", "private-internal-hash", "private form value",
        "operation_id", "tool_call_id", "started_at", "completed_at",
        "?private=yes", "#fragment",
    ):
        assert private not in serialized

    ambiguous = action_result(
        ok=False,
        receipt=make_receipt(
            before_state=after,
            after_state=after,
            dispatch_state=DispatchState.EFFECT_UNKNOWN,
            outcome=ReceiptOutcome.OUTCOME_UNKNOWN,
        ),
        error_code="pagination_postcondition_timeout",
    )
    ambiguous_projection = json.loads(
        agent_module._model_result_text(ambiguous, "paginate_browser")
    )
    # WOR-305: uncertainty is factual feedback, never a veto on a distinct
    # fresh selected-model request.
    assert "must_not_replay" not in ambiguous_projection
    assert "safe_retry" not in ambiguous_projection

    stale = action_result(
        ok=False,
        receipt=make_receipt(
            before_state=after,
            after_state=None,
            dispatch_state=DispatchState.NOT_DISPATCHED,
            outcome=ReceiptOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT,
        ),
        error_code="stale_element_reference",
    )
    stale_projection = json.loads(
        agent_module._model_result_text(stale, "activate_browser_element")
    )
    assert stale_projection["safe_retry"] is True
    assert stale_projection["stale_reference_guidance"] == {
        "reobserve_with": "get_browser_state",
        "then": "inspect_browser_page",
        "may_replay_stale_action": False,
    }
    assert agent_module._model_result_text({"text": "legacy result"}) == (
        "legacy result"
    )


def _run_fake_pagination(*, transition: bool):
    owner = browser._AsyncBrowserOwner()
    context = object()

    class FakeCandidates:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

    class FakePage:
        def __init__(self):
            self.url = "https://fixture.invalid/page-one"
            self.fingerprint = "fingerprint-one"
            self.main_frame = object()
            self.handlers = {}

        def on(self, event, handler):
            self.handlers.setdefault(event, []).append(handler)

        def off(self, event, handler):
            if handler in self.handlers.get(event, []):
                self.handlers[event].remove(handler)

        def locator(self, _selector):
            return FakeCandidates()

    page = FakePage()
    owner._context = context
    owner._active_page = page
    owner._state_tracker.start_context(context)
    before = owner._state_tracker.register_page(page)

    class FakeLocator:
        def __init__(self):
            self.dispatches = 0

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def click(self, timeout=None):
            assert timeout == 5_000
            self.dispatches += 1
            if transition:
                current = owner._state_tracker.current(page)
                owner._bound_refs.invalidate_page(current.page_id)
                page.url = "https://fixture.invalid/page-two"
                page.fingerprint = "fingerprint-two"
                owner._state_tracker.note_navigation(page)
                for handler in tuple(page.handlers.get("framenavigated", [])):
                    handler(page.main_frame)
                raise RuntimeError("locator detached after committed navigation")

    locator = FakeLocator()
    next_ref = owner._bound_refs.issue("pagination", before, locator)

    async def resolve(state_ref, activate=False):
        assert activate is True
        resolved = owner._state_tracker.validate(state_ref)
        return resolved, owner._state_tracker.current(resolved)

    async def page_url(candidate):
        return candidate.url

    async def page_fingerprint(candidate):
        return candidate.fingerprint

    owner._resolve_state_page = resolve
    owner._page_url = page_url
    owner._page_fingerprint = page_fingerprint
    owner._reconcile_pages = lambda _context: page
    result = asyncio.run(owner._op_paginate(before.to_dict(), next_ref, 1))
    return owner, locator, before, next_ref, result


def test_pagination_accepts_verified_navigation_after_locator_becomes_stale():
    owner, locator, before, next_ref, result = _run_fake_pagination(
        transition=True,
    )
    data = _success(result)
    receipt = result["receipt"]
    assert locator.dispatches == 1
    assert data["dispatch_count"] == 1
    assert data["pages_advanced"] == 1
    assert receipt["before_state"] == before.to_dict()
    assert receipt["after_state"]["document_epoch"] == before.document_epoch + 1
    assert receipt["evidence"][0] == {
        "pagination_step": 1,
        "single_dispatch": True,
        "navigation_changed": True,
        "document_epoch_advanced": True,
        "content_changed": False,
        "dispatch_completion_error_after_verified_effect": True,
    }
    with pytest.raises(BrowserReferenceError):
        owner._bound_refs.resolve(next_ref, before)


def test_pagination_never_classifies_an_unchanged_page_as_success(monkeypatch):
    monkeypatch.setattr(
        browser, "_PAGINATION_POSTCONDITION_TIMEOUT_S", 0.1,
    )
    _owner, locator, before, _next_ref, result = _run_fake_pagination(
        transition=False,
    )
    assert locator.dispatches == 1
    assert result["ok"] is False
    assert result["receipt"]["before_state"] == before.to_dict()
    assert result["receipt"]["dispatch_state"] == "effect_unknown"
    assert result["receipt"]["outcome"] == "outcome_unknown"
    assert result["error"]["code"] == "pagination_postcondition_timeout"


def test_windows_browser_smoke_requires_private_download_verification(monkeypatch):
    launcher_path = (
        Path(__file__).resolve().parents[1] / "packaging" / "windows" / "launcher.py"
    )
    spec = importlib.util.spec_from_file_location("wor186_launcher", launcher_path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    calls = []
    monkeypatch.setattr(browser, "_detect_chromium_browser", lambda: "browser.exe")
    monkeypatch.setattr(browser, "_owner", SimpleNamespace(call=lambda *_a, **_k: {
        "ok": True, "data": {"state": {"page_id": "page_safe_ref"}},
    }))
    monkeypatch.setattr(browser, "inspect_browser_page", lambda _state: {
        "ok": True,
        "data": {
            "headings": [{"text": "OSIA browser smoke"}],
            "links": [{
                "safe_destination": "http://127.0.0.1/download",
                "element_ref": "el_safe_download_ref",
            }],
        },
    })
    monkeypatch.setattr(
        browser,
        "download_browser_file",
        lambda state, element_ref: calls.append((state, element_ref)) or {
            "ok": True,
            "data": {"artifact": {"artifact_id": "web_safe_smoke_ref"}},
        },
    )
    monkeypatch.setattr(browser, "close_browser", lambda: None)
    monkeypatch.setattr(browser, "_processes_using_profile", lambda _path: [])

    import osia.tools.browser_artifacts as browser_artifacts

    smoke_bytes = b"OSIA frozen browser smoke download\n"

    class FakeStore:
        def __init__(self, _root):
            pass

        def inspect(self, artifact_id):
            assert artifact_id == "web_safe_smoke_ref"
            return {
                "size": len(smoke_bytes),
                "sha256": hashlib.sha256(smoke_bytes).hexdigest(),
                "private": True,
                "executable": False,
                "extractable": False,
            }

    monkeypatch.setattr(browser_artifacts, "BrowserArtifactStore", FakeStore)
    assert launcher._browser_smoke() == 0
    assert calls == [({"page_id": "page_safe_ref"}, "el_safe_download_ref")]


def test_real_structured_inspection_frames_waits_and_stale_state(controlled_browser):
    web = controlled_browser.web
    state = _navigate(web.base + "/extract?token=QUERY_SENTINEL#fragment")
    inspected = _success(browser.inspect_browser_page(state))
    assert inspected["safe_url"] == web.base + "/extract"
    assert inspected["title"] == "Extraction fixture"
    assert inspected["language"] == "fr"
    assert inspected["headings"][0]["text"] == "Primary heading"
    assert inspected["sections"][0]["kind"] in {"main", "article"}
    assert inspected["tables"][0]["headers"] == ["Name", "Value"]
    assert inspected["tables"][0]["rows"][1] == ["Alpha", "1"]
    assert inspected["lists"][0]["items"] == ["First item", "Second item"]
    assert "QUERY_SENTINEL" not in json.dumps(inspected)

    stale = state
    state = _navigate(web.base + "/frames", state)
    frames = _success(browser.inspect_browser_page(state))["frames"]
    same_origin_frame = next(
        item for item in frames if item["same_origin"] and item.get("frame_ref")
    )
    framed = _success(browser.inspect_browser_page(
        state, mode="headings", frame_ref=same_origin_frame["frame_ref"],
    ))
    assert framed["headings"][0]["text"] == "Same-origin frame"
    assert any(item.get("status") == "cross_origin_inaccessible" for item in frames)
    failed = browser.inspect_browser_page(stale)
    assert failed["ok"] is False
    assert failed["error"]["code"] == "stale_browser_reference"

    state = _navigate(web.base + "/delayed-content", state)
    waited = browser.wait_for_browser_condition(state, {
        "condition_type": "text_appearance",
        "expected": "Late content ready",
        "timeout_seconds": 3,
        "quiet_period_seconds": 0.1,
    })
    _success(waited)
    _success(browser.wait_for_browser_condition(state, {
        "condition_type": "stable_content_fingerprint",
        "timeout_seconds": 2,
        "quiet_period_seconds": 0.2,
    }))
    _success(browser.wait_for_browser_condition(state, {
        "condition_type": "network_quiet",
        "timeout_seconds": 2,
        "quiet_period_seconds": 0.1,
    }))
    state = _navigate(web.base + "/spa", state)
    _success(browser.wait_for_browser_condition(state, {
        "condition_type": "text_appearance",
        "expected": "Delayed dynamic content ready",
        "timeout_seconds": 3,
        "quiet_period_seconds": 0.1,
    }))
    assert "Delayed dynamic content ready" in _success(
        browser.inspect_browser_page(state, mode="main_content")
    )["sections"][0]["text"]


def test_real_chat_agent_bridge_uses_typed_browser_results(controlled_browser):
    from osia import agent as agent_module

    navigated = agent_module.tool_navigate_browser(
        controlled_browser.web.base + "/static"
    )
    state = _success(navigated)["state"]
    inspected = agent_module.tool_inspect_browser_page(state, mode="headings")
    data = _success(inspected)
    assert data["headings"][0]["text"] == "Fixture heading"
    assert "screenshot" not in json.dumps((navigated, inspected)).lower()


def test_exact_browser_extraction_and_nested_scroll_with_mouse_disabled(
    controlled_browser, monkeypatch,
):
    from osia.agent import _execute_tool_with_policy
    from osia.slash_commands import chat_command_scope
    from osia.tools import desktop_control

    state = _navigate(controlled_browser.web.base + "/audit-browser")
    snapshot = _success(browser.inspect_browser_page(state))
    table_ref = snapshot["tables"][1]["element_ref"]
    table = _success(browser.inspect_browser_page(
        state, mode="table", target_ref=table_ref, max_items=1,
    ))
    assert table["status"] == "truncated"
    assert table["tables"] == [{
        "caption": "Exact metrics", "headers": [], "rows": [["Second table"]],
        "element_ref": table_ref,
    }]
    assert not table["lists"]
    list_ref = snapshot["lists"][0]["element_ref"]
    listing = _success(browser.inspect_browser_page(state, mode="list", target_ref=list_ref))
    assert listing["lists"][0]["items"] == ["Exact list"]
    mismatch = browser.inspect_browser_page(state, mode="list", target_ref=table_ref)
    assert mismatch["error"]["code"] == "target_does_not_match_extraction_mode"

    panel = next(region for region in snapshot["scroll_regions"] if region["label"] == "Nested panel")
    assert panel["scroll_top"] == 0
    monkeypatch.setattr("osia.conversations.conversation_special_mode", lambda _: True)
    monkeypatch.setattr(
        desktop_control, "focus_app_window", lambda *_: pytest.fail("desktop focus used"),
    )
    with chat_command_scope("browser-scroll-fixture", None):
        result = _execute_tool_with_policy(
            "scroll", {"direction": "down", "amount": 120, "state_ref": state,
                       "element_ref": panel["element_ref"]},
            allow_retry=False,
        )
    assert _success(result.result) == {"movement": 120, "nested": True, "status": "moved"}
    updated = _success(browser.inspect_browser_page(state))
    assert next(
        region["scroll_top"] for region in updated["scroll_regions"] if region["label"] == "Nested panel"
    ) == 120


def test_browser_rejects_replaced_dom_node_before_acting(controlled_browser):
    state = _navigate(controlled_browser.web.base + "/audit-browser")
    before = _success(browser.inspect_browser_page(state))
    old_ref = next(
        item["element_ref"] for item in before["buttons"] if item["text"] == "Run action"
    )
    _success(browser.click("#replace"))
    expected = {
        "condition_type": "text_appearance", "expected": "Replacement clicked",
        "timeout_seconds": 1.0,
    }
    rejected = browser.activate_browser_element(state, old_ref, expected)
    assert rejected["error"]["code"] == "stale_element_reference"
    assert rejected["receipt"]["dispatch_state"] == "not_dispatched"
    updated = _success(browser.inspect_browser_page(state))
    assert "Replacement ready" in updated["page_text"]
    new_ref = next(
        item["element_ref"] for item in updated["buttons"] if item["text"] == "Run action"
    )
    assert old_ref != new_ref
    _success(browser.activate_browser_element(state, new_ref, expected))


def test_old_frame_reference_cannot_read_cross_origin_navigation(controlled_browser):
    state = _navigate(controlled_browser.web.base + "/frame-transition")
    snapshot = _success(browser.inspect_browser_page(state))
    old_ref = snapshot["frames"][0]["frame_ref"]
    _success(browser.click("#switch-frame"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = _success(browser.inspect_browser_page(state))
        if snapshot["frames"][0].get("status") == "cross_origin_inaccessible":
            break
        time.sleep(0.05)
    assert snapshot["frames"][0]["status"] == "cross_origin_inaccessible"
    rejected = browser.inspect_browser_page(state, mode="headings", frame_ref=old_ref)
    assert rejected["error"]["code"] == "stale_element_reference"


def test_download_wait_requires_a_new_event_on_the_bound_page(controlled_browser):
    state = _navigate(controlled_browser.web.base + "/download-page")
    page = _success(browser.inspect_browser_page(state))
    download_ref = page["links"][0]["element_ref"]
    _success(browser.download_browser_file(state, download_ref))
    missed = browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
        {"condition_type": "download_completion", "timeout_seconds": 0.25},
    )
    assert missed["error"]["code"] == "postcondition_not_observed"
    assert missed["receipt"]["outcome"] == "outcome_unknown"
    _success(browser.activate_browser_element(
        state, download_ref,
        {"condition_type": "download_completion", "timeout_seconds": 2},
    ))


def test_tab_listing_pairs_navigation_state_with_the_observed_url():
    owner = browser._AsyncBrowserOwner()
    owner._state_tracker.start_context(object())

    class RedirectedTab:
        url = "https://example.test/initial"

        async def title(self):
            self.url = "https://example.test/redirected"
            owner._state_tracker.note_navigation(self)
            return "Redirected tab"

    page = RedirectedTab()
    before = owner._state_tracker.register_page(page)

    async def resolve(_state_ref):
        return page, before

    owner._resolve_state_page = resolve
    tab = _success(asyncio.run(owner._op_list_tabs()))["tabs"][0]
    assert tab["safe_url"] == "https://example.test/redirected"
    assert tab["state"]["document_epoch"] == before.document_epoch + 1
    assert owner._state_tracker.validate(tab["state"]) is page


def test_account_tools_fill_and_submit_managed_form_with_mouse_disabled(
    controlled_browser, monkeypatch, tmp_path,
):
    from osia.agent import OSIAAgent, _execute_tool_with_policy
    from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
    from osia.slash_commands import chat_command_scope
    from osia.tool_attempts import ToolAttemptStore
    from osia.tools import desktop_control

    # Run the same isolated browser fixture without creating a desktop window.
    popen = browser.subprocess.Popen

    def launch(cmd, *args, **kwargs):
        if isinstance(cmd, list) and f"--user-data-dir={controlled_browser.profile}" in cmd:
            cmd = [*cmd, "--headless=new"]
        return popen(cmd, *args, **kwargs)

    monkeypatch.setattr(browser.subprocess, "Popen", launch)
    monkeypatch.setattr("osia.conversations.conversation_special_mode", lambda _: True)
    monkeypatch.setattr(
        desktop_control, "focus_app_window", lambda *_: pytest.fail("desktop focus used"),
    )
    store = ToolAttemptStore(tmp_path / "attempts")
    client = AppServerClient(base_dir=tmp_path / "codex", tool_attempt_store=store)
    sent = []
    client._send = sent.append
    client._send_request_no_wait = lambda *_: None
    agent = OSIAAgent(model="fixture", lang="en")
    agent.provider = "chatgpt_account"
    agent.configure_tool_attempts(
        conversation_id="browser-fixture", turn_id="fixture-chat-turn",
        request_id="fixture-request", final_response_message_id="fixture-final",
    )

    def call(tool, arguments):
        call_id = f"fixture-{tool}"
        attempt = _TurnAttempt(
            thread_id="fixture-thread", turn_id=call_id,
            osia_turn_id="fixture-chat-turn", conversation_id="browser-fixture",
            client_request_id="fixture-request", model_id="fixture",
        )
        client._attempts[attempt.thread_id] = attempt
        client._handle_server_request({
            "id": len(sent) + 1,
            "method": "item/tool/call",
            "params": {
                "threadId": attempt.thread_id, "turnId": attempt.turn_id,
                "callId": call_id, "tool": tool, "arguments": arguments,
            },
        })
        event, payload = attempt.events.get_nowait()
        if tool in tool_contracts.BROWSER_VERIFY_TOOL_NAMES:
            assert event == "dynamic_tool_call"
            client._resolve_dynamic_tool_call(attempt, payload)
            assert not attempt.interrupt_sent
            reply = sent[-1]["result"]
            assert reply["success"], reply
            envelope = json.loads(reply["contentItems"][0]["text"])
            return json.loads(envelope["result"])

        assert event == "unsafe"
        assert attempt.native_tool_call == {"tool": tool, "arguments": arguments, "call_id": call_id}
        context = agent._new_tool_attempt_context(
            tool_name=tool, arguments=arguments, provider_native_call_id=call_id,
        )
        context.update(store=store, terminal_callback=None)
        result = _execute_tool_with_policy(tool, arguments, attempt_context=context)
        assert not result.is_error, result.result
        return result.result

    with chat_command_scope("browser-fixture", None):
        _navigate(controlled_browser.web.base + "/form")
        fields = call("inspect_form_fields", {})["fields"]
        name_ref = next(
            field["field_ref"] for field in fields if field.get("html_name") == "name"
        )
        fill = call("fill_form_field", {"field_ref": name_ref, "value": "Projet test"})
        assert json.loads(fill)["verified"]
        state = call("get_browser_state", {})["current_page_ref"]
        page = call("inspect_browser_page", {"state_ref": state})["operation_data"]
        submit_ref = page["buttons"][0]["element_ref"]
        result = call("activate_browser_element", {
            "state_ref": state, "element_ref": submit_ref,
            "wait_spec": {"condition_type": "navigation_commit", "timeout_seconds": 4},
        })
        _success(result)
        assert result["receipt"]["after_state"]["navigation_id"] != state["navigation_id"]


def test_real_extraction_menu_consent_confirm_and_prompt(controlled_browser):
    web = controlled_browser.web
    state = _navigate(web.base + "/extract")
    for mode in (
        "main_content", "article", "headings", "links", "metadata",
        "table", "list", "page_text_chunk",
    ):
        extracted = _success(browser.inspect_browser_page(state, mode=mode))
        assert extracted["status"] in {"ok", "truncated"}

    state = _navigate(web.base + "/menu", state)
    menu = _success(browser.inspect_browser_page(state))
    menu_button = menu["buttons"][0]["element_ref"]
    _success(browser.activate_browser_element(state, menu_button, {
        "condition_type": "element_state",
        "expected": {"element_ref": menu_button, "state": "expanded"},
        "timeout_seconds": 2,
        "quiet_period_seconds": 0.1,
    }))

    state = _navigate(web.base + "/consent", state)
    consent = _success(browser.inspect_browser_page(state))
    _success(browser.activate_browser_element(
        state, consent["buttons"][0]["element_ref"],
        {
            "condition_type": "dom_selector_state",
            "expected": {"primitive": "dialog", "state": "hidden"},
            "timeout_seconds": 2,
        },
    ))

    state = _navigate(web.base + "/dialogs", state)
    dialog_page = _success(browser.inspect_browser_page(state))
    for index, action, prompt_text in (
        (1, "dismiss", None),
        (2, "accept", PRIVATE_SENTINEL),
    ):
        _success(browser.activate_browser_element(
            state, dialog_page["buttons"][index]["element_ref"],
            {
                "condition_type": "dialog_appearance",
                "timeout_seconds": 2,
            },
        ))
        pending = _success(browser.list_browser_dialogs(state))["dialogs"]
        assert len(pending) == 1
        handled = browser.handle_browser_dialog(
            state, pending[0]["dialog_ref"], action, prompt_text,
        )
        _success(handled)
        assert PRIVATE_SENTINEL not in json.dumps(handled)


def test_real_search_pagination_history_tabs_and_infinite_scroll(controlled_browser):
    web = controlled_browser.web
    search_result = browser.search_web("controlled result")
    search = _success(search_result)
    assert (
        search_result["receipt"]["after_state"]["document_epoch"]
        > search_result["receipt"]["before_state"]["document_epoch"]
    )
    assert len(search["results"]) == 1
    state = search["state"]
    opened = _success(browser.open_search_result(
        state, search["results"][0]["result_ref"],
    ))
    state = opened["state"]
    assert _success(browser.inspect_browser_page(state))["title"] == "Static fixture"

    state = _navigate(web.base + "/page/1", state)
    page = _success(browser.inspect_browser_page(state))
    repeated_page = _success(browser.inspect_browser_page(state))
    assert repeated_page["links"][0]["element_ref"] == (
        page["links"][0]["element_ref"]
    )
    paged = _success(browser.paginate_browser(
        state, page["links"][0]["element_ref"], max_pages=3,
    ))
    assert paged["pages_advanced"] == 1
    assert paged["status"] == "missing_next_control"

    state = _navigate(web.base + "/history/a", paged["state"])
    history = _success(browser.inspect_browser_page(state))
    state = _success(browser.activate_browser_element(
        state, history["links"][0]["element_ref"],
        {
            "condition_type": "url_match",
            "expected": "/history/b",
            "timeout_seconds": 2,
        },
    ))["state"]
    state = _success(browser.browser_back(state))["state"]
    state = _success(browser.browser_forward(state))["state"]
    state = _success(browser.reload_browser_page(state))["state"]
    new_tab = _success(browser.open_new_browser_tab(web.base + "/static", state))
    tabs = _success(browser.list_browser_tabs(new_tab["state"]))["tabs"]
    assert len(tabs) >= 2
    original_tab = next(item for item in tabs if not item["active"])
    _success(browser.select_browser_tab(original_tab["state"]))
    _success(browser.select_browser_tab(new_tab["state"]))
    closed = _success(browser.close_browser_tab(new_tab["state"]))
    assert closed["active_state"] is not None

    state = _navigate(web.base + "/infinite", closed["active_state"])
    scrolled = _success(browser.scroll_infinite(state, max_steps=5))
    assert scrolled["steps"] <= 5
    assert scrolled["status"] in {"reached_end", "no_progress", "max_steps"}


def test_real_upload_exact_binding_without_authorization(
    controlled_browser, tmp_path,
):
    web = controlled_browser.web
    state = _navigate(web.base + "/form")
    inspected = _success(browser.inspect_browser_page(state))
    file_control = next(
        item for item in inspected["controls"] if item["type"] == "file"
    )
    base_arguments = {
        "state_ref": state,
        "element_ref": file_control["element_ref"],
        "local_path": str(web.upload_file.resolve()),
    }
    prepared = browser.prepare_upload_authorization(dict(base_arguments))
    assert prepared == browser.prepare_upload_authorization(dict(base_arguments))
    assert str(web.upload_file) not in json.dumps(prepared)

    # WOR-301: the upload dispatches without any user authorization provider.
    previous_provider = mcp_registry.get_confirmation_provider()
    mcp_registry.set_confirmation_provider(None)
    try:
        assert action_policy.guard_tool_call(
            "Upload this exact synthetic file to the controlled form.",
            "upload_browser_file", dict(base_arguments),
        ) is None
    finally:
        mcp_registry.set_confirmation_provider(previous_provider)

    original_upload = web.upload_file.read_bytes()
    try:
        web.upload_file.write_bytes(original_upload + b"changed")
        changed = browser.upload_browser_file(
            **base_arguments, **prepared,
        )
        assert changed["ok"] is False
        assert changed["error"]["code"] == "file_changed"
        assert changed["receipt"]["dispatch_state"] == "not_dispatched"
    finally:
        web.upload_file.write_bytes(original_upload)


def test_real_single_dispatch_popup_dialog_upload_and_download(controlled_browser):
    web = controlled_browser.web
    state = _navigate(web.base + "/duplicate")
    page = _success(browser.inspect_browser_page(state))
    _success(browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
        {
            "condition_type": "text_appearance",
            "expected": "Counted once",
            "timeout_seconds": 2,
        },
    ))
    deadline = time.monotonic() + 2
    while web.duplicate_count < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert web.duplicate_count == 1

    state = _navigate(web.base + "/ambiguous")
    page = _success(browser.inspect_browser_page(state))
    ambiguous = browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
        {
            "condition_type": "text_appearance",
            "expected": "Never appears",
            "timeout_seconds": 0.2,
            "quiet_period_seconds": 0.05,
        },
    )
    assert ambiguous["receipt"]["outcome"] == "outcome_unknown"
    assert ambiguous["receipt"]["dispatch_state"] == "effect_unknown"
    deadline = time.monotonic() + 2
    while web.duplicate_count < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert web.duplicate_count == 2

    state = _navigate(web.base + "/popup-delay")
    page = _success(browser.inspect_browser_page(state))
    popup = _success(browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
        {
            "condition_type": "popup_creation",
            "timeout_seconds": 2,
        },
    ))
    deadline = time.monotonic() + 2
    popup_state = popup["state"]
    while time.monotonic() < deadline:
        tabs = _success(browser.list_browser_tabs())["tabs"]
        matching = [item for item in tabs if item["safe_url"].endswith("/popup")]
        if matching:
            popup_state = matching[0]["state"]
            break
        time.sleep(0.02)
    assert popup_state["page_id"] != state["page_id"]
    opener = _success(browser.return_to_browser_opener(popup_state))["state"]
    assert opener["page_id"] == state["page_id"]
    _success(browser.close_browser_tab(popup_state))

    state = _navigate(web.base + "/dialogs", opener)
    page = _success(browser.inspect_browser_page(state))
    _success(browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
        {
            "condition_type": "dialog_appearance",
            "timeout_seconds": 2,
        },
    ))
    dialogs = _success(browser.list_browser_dialogs(state))["dialogs"]
    assert dialogs and dialogs[0]["type"] == "alert"
    handled = browser.handle_browser_dialog(
        state, dialogs[0]["dialog_ref"], "accept",
    )
    assert "private dialog body" not in json.dumps(handled)
    _success(handled)

    state = _navigate(web.base + "/form", state)
    page = _success(browser.inspect_browser_page(state))
    file_control = next(item for item in page["controls"] if item["type"] == "file")
    uploaded = browser.upload_browser_file(
        state, file_control["element_ref"],
        local_path=str(web.upload_file.resolve()),
    )
    _success(uploaded)
    serialized_upload = json.dumps(uploaded)
    assert web.upload_file.name not in serialized_upload
    assert str(web.upload_file) not in serialized_upload
    assert "benign synthetic upload" not in serialized_upload

    state = _navigate(web.base + "/download-page", state)
    page = _success(browser.inspect_browser_page(state))
    phases = []
    browser.set_browser_progress_callback(lambda item: phases.append(dict(item)))
    downloaded = browser.download_browser_file(
        state, page["links"][0]["element_ref"],
    )
    artifact = _success(downloaded)["artifact"]
    browser.set_browser_progress_callback(None)
    assert artifact["size"] == len(DOWNLOAD_BYTES)
    assert "sha256" not in artifact
    assert artifact["mime_type"] in {"text/plain", "application/octet-stream"}
    assert [item["phase"] for item in phases] == [
        "waiting", "downloading", "finalizing", "complete",
    ]
    serialized_download = json.dumps(downloaded)
    assert "synthetic.txt" not in serialized_download
    assert DOWNLOAD_BYTES.decode().strip() not in serialized_download


def test_real_download_preflight_and_browser_restart_cancel_cleanup(controlled_browser):
    web = controlled_browser.web
    state = _navigate(web.base + "/oversized-page")
    inspected = _success(browser.inspect_browser_page(state))
    rejected = browser.download_browser_file(
        state, inspected["links"][0]["element_ref"],
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "file_size_limit_exceeded"
    assert rejected["receipt"]["dispatch_state"] == "not_dispatched"

    state = _navigate(web.base + "/download-page", state)
    inspected = _success(browser.inspect_browser_page(state))
    controlled_browser.owner._download_session_bytes = MAX_SESSION_TEMP_BYTES
    quota = browser.download_browser_file(
        state, inspected["links"][0]["element_ref"],
    )
    controlled_browser.owner._download_session_bytes = 0
    assert quota["ok"] is False
    assert quota["error"]["code"] == "browser_session_quota_exceeded"
    assert quota["receipt"]["dispatch_state"] == "dispatched"

    state = _navigate(web.base + "/download-delay-page", state)
    inspected = _success(browser.inspect_browser_page(state))
    phases = []
    browser.set_browser_progress_callback(lambda item: phases.append(dict(item)))
    results = []

    def run_download():
        try:
            results.append(browser.download_browser_file(
                state, inspected["links"][0]["element_ref"],
            ))
        except BaseException as exc:
            results.append(exc)

    worker = threading.Thread(target=run_download)
    worker.start()
    temp_root = Path(tempfile.gettempdir()) / "osia-browser-downloads"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if temp_root.exists() and any(temp_root.iterdir()):
            break
        time.sleep(0.02)
    browser.close_browser()
    worker.join(timeout=10)
    browser.set_browser_progress_callback(None)
    assert not worker.is_alive()
    assert any(item["phase"] == "cancelled" for item in phases)
    assert not temp_root.exists() or not any(temp_root.iterdir())

    restarted = _navigate(web.base + "/static")
    assert _success(browser.inspect_browser_page(restarted))["title"] == "Static fixture"
    retired = browser.inspect_browser_page(state)
    assert retired["ok"] is False
    assert retired["error"]["code"] == "stale_browser_reference"


@pytest.mark.parametrize(
    ("path", "reason"),
    (("/login", "manual_login_required"),
     ("/captcha", "captcha_required"),
     ("/2fa", "two_factor_required")),
)
def test_real_manual_intervention_is_detected_without_bypass(
    controlled_browser, path, reason,
):
    state = _navigate(controlled_browser.web.base + path)
    inspected = _success(browser.inspect_browser_page(state))
    assert inspected["manual_intervention"] == reason
    serialized = json.dumps(inspected)
    if reason == "manual_login_required":
        sensitive = [
            field for field in inspected.get("semantic_forms", {}).get("fields", [])
            if field.get("sensitive")
        ]
        assert sensitive
        assert all(not field.get("fillable") for field in sensitive)
        assert all("value" not in field for field in sensitive)
    assert PRIVATE_SENTINEL not in serialized
