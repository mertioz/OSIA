"""Controlled-browser coverage for WOR-176 semantic form operations."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import action_policy
from osia import agent as agent_module
from osia.tools import browser
from osia.tools.browser_forms import (
    FORM_FIELD_ACTION_TIMEOUT_MS,
    FORM_FILL_TIMEOUT_SECONDS,
    FORM_REFERENCE_TTL_SECONDS,
    FORM_VALIDATION_TIMEOUT_MS,
    SemanticFormSession,
)


PRIVATE_MARKER = "WOR176_PRIVATE_MARKER_DO_NOT_LEAK"
VALUE_MARKER = "WOR176_VALUE_MARKER_DO_NOT_LEAK"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _form_html(cross_port: int) -> bytes:
    long_label = "L" * 180
    fillers = "".join(
        f'<label for="f{i}">Filler {i}</label><input id="f{i}" name="filler_{i}">'
        for i in range(8)
    )
    return f"""<!doctype html><html><body>
<form id="profile" onsubmit="event.preventDefault(); window.submitCount += 1">
  <label for="full">Full name</label>
  <input id="full" name="full_name" value="{PRIVATE_MARKER}">
  <input name="aria_field" aria-label="ARIA field">
  <span id="org-label">Organization</span>
  <input name="labelled_field" aria-labelledby="org-label">
  <input name="placeholder_field" placeholder="Portfolio URL">
  <label>Duplicate label <input name="duplicate_a"></label>
  <label>Duplicate label <input name="duplicate_b"></label>
  <label for="notes">Notes</label><textarea id="notes" name="notes"></textarea>
  <label for="number">Count</label><input id="number" name="count" type="number">
  <label for="date">Start date</label><input id="date" name="start_date" type="date">
  <label for="country">Country</label><select id="country" name="country">
    <option value="fr">France</option><option value="de">Germany</option>
  </select>
  <label><input type="checkbox" name="agree"> Agree</label>
  <label><input type="radio" name="choice" value="one"> Choice one</label>
  <input role="combobox" name="combo" aria-label="Editable choice">
  <input name="required_field" aria-label="Required field" required>
  <input name="readonly_field" aria-label="Readonly field" readonly>
  <input name="disabled_field" aria-label="Disabled field" disabled>
  <input name="hidden_field" type="hidden" value="{PRIVATE_MARKER}">
  <input name="password" type="password" value="{PRIVATE_MARKER}">
  <input name="private_label" aria-label="{PRIVATE_MARKER}">
  <input name="private_placeholder" placeholder="{PRIVATE_MARKER}">
  <label for="long">{long_label}</label><input id="long" name="long_label">
  <label for="react">React field</label><input id="react" name="react_field">
  <label for="delayed-react">Delayed React field</label>
  <input id="delayed-react" name="delayed_react_field">
  <label for="lost">Lost React field</label><input id="lost" name="lost_react">
  <label for="delayed">Delayed validation</label><input id="delayed" name="delayed_invalid">
  <label for="mismatch">Mismatch</label><input id="mismatch" name="mismatch">
  {fillers}
  <button id="submit" type="submit">Submit application</button>
</form>
<iframe src="/frame"></iframe>
<iframe src="http://127.0.0.1:{cross_port}/cross"></iframe>
<script>
window.submitCount = 0;
document.querySelector('#react').addEventListener('input', event => {{
  const clone = event.target.cloneNode(true);
  clone.value = event.target.value;
  event.target.replaceWith(clone);
}});
document.querySelector('#delayed-react').addEventListener('input', event => {{
  setTimeout(() => {{
    const clone = event.target.cloneNode(true);
    clone.value = event.target.value;
    event.target.replaceWith(clone);
  }}, 100);
}});
document.querySelector('#lost').addEventListener('input', event => {{
  const clone = event.target.cloneNode(true);
  clone.value = event.target.value;
  document.body.appendChild(clone);
  event.target.remove();
}});
document.querySelector('#delayed').addEventListener('input', event => {{
  setTimeout(() => {{
    event.target.setCustomValidity('{PRIVATE_MARKER}');
    event.target.setAttribute('aria-invalid', 'true');
  }}, 80);
}});
document.querySelector('#mismatch').addEventListener('input', event => {{
  setTimeout(() => {{ event.target.value = ''; }}, 80);
}});
</script>
</body></html>""".encode("utf-8")


class _MainHandler(BaseHTTPRequestHandler):
    cross_port = 0

    def do_GET(self):
        if self.path.startswith("/frame"):
            body = (
                b'<!doctype html><label for="iframe">Iframe field</label>'
                b'<input id="iframe" name="iframe_field">'
            )
        elif self.path.startswith("/popup"):
            body = (
                b'<!doctype html><label for="popup">Popup field</label>'
                b'<input id="popup" name="popup_field">'
            )
        elif self.path.startswith("/opener"):
            body = b"""<!doctype html><button id="open" onclick="window.open('/popup')">
Open popup</button>"""
        elif self.path.startswith("/next"):
            body = b"<!doctype html><body>next page</body>"
        else:
            body = _form_html(self.cross_port)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


class _CrossHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            b'<!doctype html><label for="cross">Cross field</label>'
            b'<input id="cross" name="cross_origin_field">'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


@pytest.fixture(scope="module")
def controlled_browser(tmp_path_factory):
    binary = browser.resolve_browser_name("brave")
    if not binary:
        pytest.skip("Brave is unavailable for controlled browser validation")
    http_port, cross_port, cdp_port = _free_port(), _free_port(), _free_port()
    _MainHandler.cross_port = cross_port
    main_server = ThreadingHTTPServer(("127.0.0.1", http_port), _MainHandler)
    cross_server = ThreadingHTTPServer(("127.0.0.1", cross_port), _CrossHandler)
    threads = [
        threading.Thread(target=main_server.serve_forever, daemon=True),
        threading.Thread(target=cross_server.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()

    original = {
        "owner": browser._owner,
        "cdp_port": browser.CDP_PORT,
        "cdp_url": browser.CDP_URL,
        "profile": browser.OSIA_PROFILE_DIR,
        "detect": browser._detect_chromium_browser,
        "raise": browser._raise_browser_window,
    }
    owner = browser._AsyncBrowserOwner()
    profile = tmp_path_factory.mktemp("wor176-browser") / "profile"
    browser._owner = owner
    browser.CDP_PORT = cdp_port
    browser.CDP_URL = f"http://127.0.0.1:{cdp_port}"
    browser.OSIA_PROFILE_DIR = str(profile)
    browser._detect_chromium_browser = lambda: binary
    browser._raise_browser_window = lambda _kind=None: None
    base = f"http://127.0.0.1:{http_port}"
    try:
        yield SimpleNamespace(base=base, owner=owner, profile=profile)
    finally:
        browser.close_browser()
        leftovers = browser._processes_using_profile(str(profile))
        if leftovers:
            browser._terminate_processes(leftovers)
        browser._owner = original["owner"]
        browser.CDP_PORT = original["cdp_port"]
        browser.CDP_URL = original["cdp_url"]
        browser.OSIA_PROFILE_DIR = original["profile"]
        browser._detect_chromium_browser = original["detect"]
        browser._raise_browser_window = original["raise"]
        for server in (main_server, cross_server):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        shutil.rmtree(profile.parent, ignore_errors=True)
    assert not browser._processes_using_profile(str(profile))
    assert not profile.parent.exists()


def _navigate(controlled_browser, path="/form"):
    result = browser.navigate(controlled_browser.base + path)
    assert isinstance(result, dict), result
    assert result["text"].startswith("Navigated to:")


def _snapshot(**kwargs) -> tuple[str, dict]:
    raw = browser.inspect_form_fields(**kwargs)
    assert not raw.startswith("ERROR:"), raw
    return raw, json.loads(raw)


def _by_name(snapshot: dict, name: str) -> dict:
    matches = [item for item in snapshot["fields"] if item.get("html_name") == name]
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _error_category(result: str) -> str:
    assert result.startswith("ERROR:"), result
    return json.loads(result.split(":", 1)[1])["category"]


def test_semantic_fill_budget_chain_is_strictly_nested():
    assert FORM_FIELD_ACTION_TIMEOUT_MS == 5000
    assert FORM_VALIDATION_TIMEOUT_MS == 5000
    assert FORM_FILL_TIMEOUT_SECONDS == 18.0
    assert FORM_FILL_TIMEOUT_SECONDS < 22.0
    assert browser._owner is not None
    assert agent_module._configured_tool_timeout_seconds(
        agent_module._tool_spec_and_contract(
            "fill_form_field",
            {"field_ref": "fld_AAAAAAAAAAAAAAAAAAAAAAAA", "value": "x"},
        )[0],
        "fill_form_field",
        {},
    ) == 24.0


def test_bounded_semantic_snapshot_frames_duplicates_and_no_values(controlled_browser):
    _navigate(controlled_browser)
    raw, snapshot = _snapshot(max_fields=100)
    assert PRIVATE_MARKER not in raw
    assert VALUE_MARKER not in raw
    assert snapshot["ok"] is True
    names = {item.get("html_name") for item in snapshot["fields"]}
    assert {
        "full_name", "aria_field", "labelled_field", "placeholder_field",
        "duplicate_a", "duplicate_b", "notes", "country", "agree", "choice",
        "combo", "required_field", "readonly_field", "password", "iframe_field",
    } <= names
    assert "disabled_field" not in names
    assert "hidden_field" not in names
    assert "cross_origin_field" not in names
    assert _by_name(snapshot, "full_name")["accessible_label"] == "Full name"
    assert _by_name(snapshot, "aria_field")["label_source"] == "aria-label"
    assert _by_name(snapshot, "labelled_field")["label_source"] == "aria-labelledby"
    placeholder = _by_name(snapshot, "placeholder_field")
    assert placeholder["placeholder"] == "Portfolio URL"
    assert placeholder["label_source"] == "placeholder"
    for name in ("duplicate_a", "duplicate_b"):
        assert _by_name(snapshot, name)["duplicate_count"] == 2
    assert {
        _by_name(snapshot, "duplicate_a")["duplicate_ordinal"],
        _by_name(snapshot, "duplicate_b")["duplicate_ordinal"],
    } == {1, 2}
    assert _by_name(snapshot, "readonly_field")["readonly"] is True
    assert _by_name(snapshot, "required_field")["required"] is True
    password = _by_name(snapshot, "password")
    assert password["sensitive"] is True
    assert password["fillable"] is False
    assert password["has_value"] is True
    assert all("value" not in item for item in snapshot["fields"])
    assert _by_name(snapshot, "iframe_field")["frame_depth"] == 1
    warnings = {item["code"]: item["count"] for item in snapshot["warnings"]}
    assert warnings["cross_origin_frame_inaccessible"] == 1
    assert warnings["sensitive_metadata_redacted"] >= 2
    assert snapshot["truncated"]["text"] is True
    assert len(_by_name(snapshot, "long_label")["accessible_label"]) == 120

    _, inclusive = _snapshot(
        include_hidden=True, include_disabled=True, max_fields=100,
    )
    inclusive_names = {item.get("html_name") for item in inclusive["fields"]}
    assert {"disabled_field", "hidden_field"} <= inclusive_names
    assert _by_name(inclusive, "disabled_field")["enabled"] is False
    assert _by_name(inclusive, "hidden_field")["visible"] is False

    _, limited = _snapshot(max_fields=3)
    assert limited["field_count"] == 3
    assert limited["truncated"]["fields"] is True


def test_verified_fill_control_types_rerenders_and_safe_failures(controlled_browser, capsys):
    _navigate(controlled_browser)
    _, snapshot = _snapshot(
        include_hidden=True, include_disabled=True, max_fields=100,
    )
    refs = {
        item.get("html_name"): item["field_ref"] for item in snapshot["fields"]
        if item.get("html_name")
    }

    text_result = browser.fill_form_field(refs["full_name"], VALUE_MARKER)
    assert VALUE_MARKER not in text_result
    parsed = json.loads(text_result)
    assert parsed["verified"] is True
    assert parsed["changed"] is True
    assert parsed["normalized_length"] == len(VALUE_MARKER)
    assert "screenshot" not in parsed
    assert parsed["receipt"] == {
        "dispatch_state": "effect_confirmed",
        "outcome": "confirmed_success",
        "reason_code": "semantic_fill_verified",
        "schema_version": 1,
    }
    repeated = json.loads(browser.fill_form_field(refs["full_name"], VALUE_MARKER))
    assert repeated["verified"] is True
    assert repeated["changed"] is False
    assert repeated["state_token"] == parsed["state_token"]

    cases = [
        ("notes", "multiline\r\ntext", "fill"),
        ("count", "42", "fill"),
        ("start_date", "2026-07-25", "fill"),
        ("country", "Germany", "select"),
        ("agree", True, "check"),
        ("choice", True, "check"),
        ("combo", "editable", "fill"),
    ]
    for name, value, operation in cases:
        result = json.loads(browser.fill_form_field(refs[name], value))
        assert result["verified"] is True
        assert result["operation"] == operation

    react = json.loads(browser.fill_form_field(refs["react_field"], "react-value"))
    assert react["verified"] is True
    assert react["reresolved"] is True
    delayed_react = json.loads(
        browser.fill_form_field(refs["delayed_react_field"], "delayed-react-value")
    )
    assert delayed_react["verified"] is True
    assert delayed_react["reresolved"] is True
    lost_result = browser.fill_form_field(refs["lost_react"], "lost-value")
    assert _error_category(lost_result) == "unverified_result"
    lost_receipt = json.loads(lost_result.split(":", 1)[1])["receipt"]
    assert lost_receipt["dispatch_state"] == "effect_unknown"
    assert lost_receipt["outcome"] == "outcome_unknown"
    assert _error_category(
        browser.fill_form_field(refs["country"], "not-an-option")
    ) == "invalid_option"
    assert _error_category(
        browser.fill_form_field(refs["delayed_invalid"], "synthetic")
    ) == "validation_rejected"
    assert _error_category(
        browser.fill_form_field(refs["mismatch"], "synthetic")
    ) == "postcondition_mismatch"
    disabled_result = browser.fill_form_field(refs["disabled_field"], "synthetic")
    assert _error_category(disabled_result) == "disabled_field"
    disabled_receipt = json.loads(disabled_result.split(":", 1)[1])["receipt"]
    assert disabled_receipt["dispatch_state"] == "not_dispatched"
    assert disabled_receipt["outcome"] == "confirmed_failure_before_effect"
    assert _error_category(
        browser.fill_form_field(refs["readonly_field"], "synthetic")
    ) == "readonly_field"
    assert _error_category(
        browser.fill_form_field(refs["hidden_field"], "synthetic")
    ) == "hidden_field"
    assert _error_category(
        browser.fill_form_field(refs["password"], "synthetic")
    ) == "sensitive_field_not_authorized"
    assert _error_category(
        browser.fill_form_field("fld_AAAAAAAAAAAAAAAAAAAAAAAA", "synthetic")
    ) == "invalid_reference"
    assert _error_category(
        browser.fill_form_field("not-a-reference", "synthetic")
    ) == "invalid_reference"
    composite_marker = "PRIVATE_OBJECT_VALUE_MUST_NOT_LEAK"
    composite_result = browser.fill_form_field(
        refs["full_name"], {"private": composite_marker},
    )
    assert _error_category(composite_result) == "invalid_value"
    assert composite_marker not in composite_result

    async def submit_count():
        return await controlled_browser.owner._active_page.evaluate("window.submitCount")

    controlled_browser.owner._op_test_submit_count = submit_count
    assert controlled_browser.owner.call("test_submit_count") == 0
    assert VALUE_MARKER not in capsys.readouterr().out


def test_navigation_page_replacement_and_popup_close_invalidate_references(
    controlled_browser,
):
    _navigate(controlled_browser)
    _, snapshot = _snapshot(max_fields=100)
    old_ref = _by_name(snapshot, "full_name")["field_ref"]
    _navigate(controlled_browser, "/next")
    assert _error_category(browser.fill_form_field(old_ref, "synthetic")) == (
        "stale_reference"
    )

    _navigate(controlled_browser, "/opener")
    browser.click("#open")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if controlled_browser.owner.snapshot().get("activeUrl", "").endswith("/popup"):
            break
        time.sleep(0.02)
    assert controlled_browser.owner.snapshot()["activeUrl"].endswith("/popup")
    _, popup_snapshot = _snapshot()
    popup_ref = _by_name(popup_snapshot, "popup_field")["field_ref"]

    async def close_active_popup():
        await controlled_browser.owner._active_page.close()
        await asyncio.sleep(0)

    controlled_browser.owner._op_test_close_active_popup = close_active_popup
    controlled_browser.owner.call("test_close_active_popup")
    assert _error_category(
        browser.fill_form_field(popup_ref, "synthetic")
    ) == "stale_reference"


def test_reference_ttl_and_ambiguous_reresolution_are_fail_closed():
    now = [10.0]
    session = SemanticFormSession(clock=lambda: now[0])
    field_ref = session._new_ref()
    session._references[field_ref] = {
        "expires_at": now[0] + FORM_REFERENCE_TTL_SECONDS,
    }
    assert session._entry(field_ref)[0] is not None
    now[0] += FORM_REFERENCE_TTL_SECONDS + 1
    assert session._entry(field_ref) == (None, "expired_reference")

    metadata = {
        "connected": True,
        "tag": "input",
        "role": "textbox",
        "label": "Duplicate",
        "name": "duplicate",
        "inputType": "text",
        "autocomplete": "",
        "structural": "body:1>input:1",
    }

    class Candidate:
        async def evaluate(self, _expression):
            return dict(metadata)

        async def element_handle(self):
            return object()

    class Locator:
        async def count(self):
            return 2

        def nth(self, _index):
            return Candidate()

    class Frame:
        def locator(self, _selector):
            return Locator()

    session._token = lambda *_args, **_kwargs: "same-structure"
    entry = {
        "frame": Frame(),
        "signature": (
            "input", "textbox", "Duplicate", "duplicate", "text", "",
        ),
        "structural_hash": "same-structure",
        "tag": "input",
        "input_type": "text",
    }
    replacement, error = asyncio.run(session._reresolve(entry))
    assert replacement is None
    assert error == "ambiguous_reference"


def test_final_form_actions_proceed_without_confirmation_and_never_blindly_retried(
    monkeypatch,
):
    request = "Submit the prepared job application form."
    assert action_policy.guard_tool_call(
        request, "find_and_click", {"text": "Submit application"},
    ) is None
    assert action_policy.is_final_external_tool_call(
        request, "press_key", {"key": "Enter"},
    ) is True

    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "find_and_click",
        lambda text: calls.append(text) or "ERROR: ambiguous final action",
    )
    result, is_error = agent_module._execute_tool_with_retry(
        "find_and_click",
        {"text": "Submit application"},
        allow_retry=False,
    )
    assert is_error is True
    assert result == "ERROR: ambiguous final action"
    assert calls == ["Submit application"]


def test_public_registration_and_legacy_browser_tools_remain_available():
    assert agent_module.TOOL_MAP["inspect_form_fields"] is (
        agent_module.tool_inspect_form_fields
    )
    assert agent_module.TOOL_MAP["fill_form_field"] is (
        agent_module.tool_fill_form_field
    )
    for name in (
        "navigate", "read_page", "get_page_text", "find_and_click", "fill",
        "type_text", "press_key", "web_state",
    ):
        assert name in agent_module.TOOL_MAP
