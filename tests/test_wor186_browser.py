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
from osia.action_policy import (
    PermissionPolicy, UsePolicy, build_permission_request, create_grant,
)
from osia.local_reauth import issue_full_access_activation_proof
from osia.mcp_client import registry as mcp_registry
from osia.permission_store import (
    BackendAvailability, PermissionStore, SecureStorageBackend,
)
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


class _HermeticProtectedBackend(SecureStorageBackend):
    @property
    def availability(self):
        return BackendAvailability(True, "wor186_test", True)

    def protect(self, plaintext: bytes) -> bytes:
        return b"W186" + bytes(value ^ 0x57 for value in plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(b"W186"):
            raise ValueError("invalid_test_blob")
        return bytes(value ^ 0x57 for value in protected[4:])


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
    assert "0x" not in json.dumps(first.to_dict())
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
        assert spec.chat_eligible is True, name
        assert spec.scheduled_eligible is False, name
        assert spec.retry.value in {
            "not_started_only", "never_after_dispatch",
            "read_once_after_confirmed_failure",
        }


def test_autonomous_browser_requires_authoritative_reference_tools():
    legacy = {
        "navigate", "click", "fill", "read_page", "get_current_url",
        "scroll", "go_back", "go_forward", "open_new_tab",
        "find_and_click", "wait_for_load", "get_page_text", "scroll_down",
        "type_text", "press_key", "open_browser", "web_state",
    }
    for name in legacy:
        assert tool_contracts.get_tool_spec(name).autonomous_eligible is False
    for name in NEW_BROWSER_TOOLS:
        assert tool_contracts.get_tool_spec(name).autonomous_eligible is True
    assert tool_contracts.get_tool_spec(
        "inspect_form_fields"
    ).autonomous_eligible is True
    assert tool_contracts.get_tool_spec("fill_form_field").autonomous_eligible is True

    from osia.autonomous.orchestrator import Orchestrator

    prompt = Orchestrator(SimpleNamespace())._compose({
        "objective": "Inspect one controlled Web page",
        "success_criterion": "The structured page state is observed",
    }, "")
    assert "navigate_browser" in prompt
    assert "inspect_browser_page" in prompt
    assert "Legacy selector-based browser tools" in prompt


def test_autonomous_web_effective_surface_is_exact_and_chat_is_unchanged():
    from osia import agent as agent_module

    expected = set(NEW_BROWSER_TOOLS) | {
        "inspect_form_fields", "fill_form_field",
    }
    forbidden = {
        "open_uri", "bash", "run_command", "navigate", "read_page",
        "click", "fill", "find_and_click", "type_text", "press_key",
        "desktop_click", "open_application", "take_screenshot",
    }
    with tool_contracts.effective_tool_surface_scope("autonomous_web"):
        specs = tool_contracts.effective_static_tool_specs()
        prompt = agent_module.get_system_prompt(
            "en", model="deepseek-v4-pro", provider="deepseek",
        )
    assert {spec.name for spec in specs} == expected
    assert all(spec.input_schema.get("type") == "object" for spec in specs)
    assert all(spec.autonomous_eligible for spec in specs)
    prompt_names = set(re.findall(r"^- ([a-z0-9_]+):", prompt, re.MULTILINE))
    assert prompt_names == expected
    assert not (forbidden & prompt_names)

    with tool_contracts.effective_tool_surface_scope(""):
        chat_names = {
            spec.name for spec in tool_contracts.effective_static_tool_specs()
        }
    assert expected <= chat_names
    assert {"open_uri", "navigate", "desktop_click", "bash"} <= chat_names

    expected_verify = {
        "inspect_form_fields", "get_browser_state", "inspect_browser_page",
        "wait_for_browser_condition", "list_browser_tabs",
        "list_browser_dialogs",
    }
    with tool_contracts.effective_tool_surface_scope(
        "autonomous_web_verify"
    ):
        verify_specs = tool_contracts.effective_static_tool_specs()
        verify_prompt = agent_module.get_system_prompt(
            "en", model="deepseek-v4-pro", provider="deepseek",
        )
    assert {spec.name for spec in verify_specs} == expected_verify
    assert set(re.findall(
        r"^- ([a-z0-9_]+):", verify_prompt, re.MULTILINE,
    )) == expected_verify
    assert not ({
        "navigate_browser", "search_web", "open_search_result",
        "activate_browser_element", "fill_form_field", "paginate_browser",
        "upload_browser_file", "download_browser_file",
    } & expected_verify)


def test_autonomous_web_bypass_tools_fail_before_dispatch():
    context = action_policy.make_execution_context(
        execution_type=action_policy.ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id="wor186-surface-run",
    )
    forbidden = (
        "open_uri", "navigate", "read_page", "click", "desktop_click",
        "bash", "run_command",
    )
    with action_policy.execution_context_scope(context), \
            tool_contracts.effective_tool_surface_scope("autonomous_web"):
        for name in forbidden:
            denied = action_policy.autonomous_permission_gate(name, {})
            assert denied == "ERROR: tool is not eligible for Autonomous execution"


def test_structured_browser_mutations_have_grantable_exact_call_authority(tmp_path):
    tracker = BrowserStateTracker()
    tracker.start_context(object())
    state_ref = tracker.register_page(object()).to_dict()
    policy = PermissionPolicy(PermissionStore(
        tmp_path / "permissions.dat", backend=_HermeticProtectedBackend(),
    ))
    context = action_policy.make_execution_context(
        execution_type=action_policy.ExecutionType.AUTONOMOUS,
        interactive=False,
        run_id="wor186-equivalence-run",
        store=policy.store,
    )
    navigation = build_permission_request(
        "navigate_browser",
        {"url": "https://fixture.invalid/start", "state_ref": state_ref},
        execution_context=context,
        force_sensitive=True,
        policy=policy,
    )
    navigation_without_optional_state = build_permission_request(
        "navigate_browser",
        {"url": "https://fixture.invalid/start"},
        execution_context=context,
        force_sensitive=True,
        policy=policy,
    )
    download = build_permission_request(
        "download_browser_file",
        {"state_ref": state_ref, "element_ref": "el_" + "a" * 32},
        execution_context=context,
        force_sensitive=True,
        policy=policy,
    )
    target_binding = "b" * 64
    activated = build_permission_request(
        "activate_browser_element",
        {
            "state_ref": state_ref,
            "element_ref": "el_" + "c" * 32,
            "trusted_target_binding": target_binding,
        },
        execution_context=context,
        force_sensitive=True,
        policy=policy,
    )
    moved_state = dict(state_ref)
    moved_state["document_epoch"] += 1
    activated_after_history = build_permission_request(
        "activate_browser_element",
        {
            "state_ref": moved_state,
            "element_ref": "el_" + "d" * 32,
            "trusted_target_binding": target_binding,
        },
        execution_context=context,
        force_sensitive=True,
        policy=policy,
    )
    for request in (navigation, download):
        assert request.private_scope.kind == "exact_call"
        assert request.private_scope.grantable is True
        assert request.private_scope.safe_category == "browser_action"
    assert navigation.normalized_call_hash != (
        navigation_without_optional_state.normalized_call_hash
    )
    assert navigation.autonomous_equivalence_hash == (
        navigation_without_optional_state.autonomous_equivalence_hash
    )
    assert activated.normalized_call_hash != (
        activated_after_history.normalized_call_hash
    )
    assert activated.autonomous_equivalence_hash == (
        activated_after_history.autonomous_equivalence_hash
    )

    from osia.autonomous import state as autonomous_state

    run = {"operation_records": [{
        "operation_id": "op_verified",
        "tool_identity": "navigate_browser",
        "normalized_call_hash": navigation.normalized_call_hash,
        "private_scope_hash": navigation.private_scope.scope_hash,
        "autonomous_equivalence_hash": navigation.autonomous_equivalence_hash,
        "status": "success_verified",
    }]}
    assert autonomous_state.equivalent_operation_record(
        run,
        tool_identity="navigate_browser",
        normalized_call_hash=navigation_without_optional_state.normalized_call_hash,
        private_scope_hash=(
            navigation_without_optional_state.private_scope.scope_hash
        ),
        autonomous_equivalence_hash=(
            navigation_without_optional_state.autonomous_equivalence_hash
        ),
        statuses={"success_verified"},
    )["operation_id"] == "op_verified"


def test_enumerated_planning_never_merges_requirements_and_blocks_subset_finish(
    monkeypatch,
):
    from osia.autonomous import planner, state as autonomous_state
    from osia.autonomous.orchestrator import Orchestrator

    objective = (
        "1. Verify the first controlled browser observation.\n"
        "2. Verify the second controlled browser observation."
    )

    class FakeAgent:
        def __init__(self, replies):
            self.replies = iter(replies)
            self.calls = 0

        def reset(self):
            return None

        def run(self, *_args, **_kwargs):
            self.calls += 1
            return next(self.replies)

    merged = json.dumps({
        "phases": [{"name": "Merged", "tasks": [{
            "requirement_id": "req_001",
            "objective": "Verify both observations",
            "success_criterion": "Both observations are visible",
        }]}],
    })
    invalid_agent = FakeAgent([merged, merged])
    assert planner.plan_objective(objective, invalid_agent) is None
    assert invalid_agent.calls == 2

    valid = json.dumps({
        "phases": [{"name": "Separate", "tasks": [
            {
                "requirement_id": "req_001",
                "objective": "Verify the first observation",
                "success_criterion": "The first observation is visible",
            },
            {
                "requirement_id": "req_002",
                "objective": "Verify the second observation",
                "success_criterion": "The second observation is visible",
            },
        ]}],
    })
    phases = planner.plan_objective(objective, FakeAgent([valid]))
    assert [
        task["requirement_id"]
        for phase in phases for task in phase["tasks"]
    ] == ["req_001", "req_002"]
    assert [
        task["success_criterion"]
        for phase in phases for task in phase["tasks"]
    ] == [
        "Verify the first controlled browser observation.",
        "Verify the second controlled browser observation.",
    ]
    run = autonomous_state.new_task_state(
        objective, "deepseek", "deepseek-v4-pro", phases,
    )
    assert [item["status"] for item in run["checklist"]] == [
        "pending", "pending",
    ]
    autonomous_state.set_task_status(run, 0, 0, "done")
    assert [item["status"] for item in run["checklist"]] == [
        "verified", "pending",
    ]

    run["cursor"] = {"phase": len(run["phases"]), "task": 0}
    run["budgets"]["max_replans"] = 0
    run["run_state"] = "running"
    orchestrator = Orchestrator(SimpleNamespace())
    orchestrator._state = run
    monkeypatch.setattr(autonomous_state, "save_task_state", lambda _state: None)
    assert orchestrator._step(run) is False
    assert run["status"] == "failed"
    assert run["run_state"] == "failed"
    assert run["failure_diagnostic"]["reason_code"] == (
        "verification_not_completed"
    )
    assert run["log"][-1]["note"] == "required_criteria_incomplete"


def test_exact_scope_resume_continues_same_agent_conversation(monkeypatch):
    from osia.autonomous import state as autonomous_state
    from osia.autonomous.orchestrator import Orchestrator

    phases = [{
        "name": "Controlled",
        "tasks": [{
            "id": "0.0",
            "requirement_id": "req_001",
            "objective": "Perform one controlled operation",
            "success_criterion": "The controlled operation is verified",
        }],
    }]
    run = autonomous_state.new_task_state(
        "1. Perform one controlled operation", "deepseek", "model", phases,
    )
    task = run["phases"][0]["tasks"][0]
    task["last_error"] = "scope_extension_required"
    run["run_state"] = "running"

    class FakeAgent:
        def __init__(self):
            self.messages = [
                {"role": "system", "content": "bounded"},
                {
                    "role": "assistant",
                    "content": "requested exact call",
                    "_autonomous_resume_call": {
                        "tool": "paginate_browser",
                        "state_ref": {"document_epoch": 4},
                        "next_ref": "E2",
                    },
                },
            ]
            self.reset_calls = 0
            self.run_messages = []
            self.exact_resume_calls = []

        def reset(self):
            self.reset_calls += 1

        def run(self, message):
            self.run_messages.append(message)
            self.exact_resume_calls.append(
                getattr(self, "_autonomous_resume_exact_call", None)
            )
            return "The exact approved operation completed."

    agent = FakeAgent()
    orchestrator = Orchestrator(agent)
    orchestrator._state = run
    orchestrator._resume_scope_extension = True
    orchestrator._resume_scope_tool = "paginate_browser"
    monkeypatch.setattr(autonomous_state, "save_task_state", lambda _state: None)
    monkeypatch.setattr(
        orchestrator,
        "_verify_outcome",
        lambda _task, _result: SimpleNamespace(
            ok=True, reason_code="verification_completed", category=None,
        ),
    )
    assert orchestrator._step(run) is True
    assert agent.reset_calls == 0
    assert len(agent.run_messages) == 1
    continuation = agent.run_messages[0]
    assert "immediately preceding assistant message" in continuation
    assert "approved operation is paginate_browser" in continuation
    assert "ignore all of them" in continuation
    assert "same arguments as your sole next tool call" in continuation
    assert "do not inspect, refresh, navigate" in continuation
    assert agent.exact_resume_calls == [{
        "tool": "paginate_browser",
        "state_ref": {"document_epoch": 4},
        "next_ref": "E2",
    }]
    assert agent._autonomous_resume_exact_call is None
    assert task["status"] == "done"


def test_suspended_batch_history_keeps_only_the_attempted_exact_call():
    from osia import agent as agent_module

    messages = [
        {"role": "system", "content": "bounded"},
        {
            "role": "assistant",
            "_provisional": True,
            "content": (
                '<tool_call>{"tool":"browser_back","state_ref":{}}</tool_call>'
                '<tool_call>{"tool":"navigate_browser","url":"private"}</tool_call>'
            ),
        },
    ]
    assert agent_module._retain_only_attempted_provisional_call(
        messages,
        "browser_back",
        {"state_ref": {"document_epoch": 9}},
    )
    retained = messages[-1]["content"]
    assert retained.count("<tool_call>") == 1
    assert '"tool": "browser_back"' in retained
    assert '"document_epoch": 9' in retained
    assert "navigate_browser" not in retained
    assert messages[-1]["_autonomous_resume_call"] == {
        "tool": "browser_back",
        "state_ref": {"document_epoch": 9},
    }


def test_approved_scope_resume_executes_only_retained_exact_call():
    from osia import agent as agent_module

    owner = SimpleNamespace(_autonomous_resume_exact_call={
        "tool": "browser_forward",
        "state_ref": {"document_epoch": 11},
    })
    selected = agent_module._apply_exact_autonomous_resume_call(owner, [
        {
            "tool": "browser_back",
            "state_ref": {"document_epoch": 9},
        },
        {"tool": "navigate_browser", "url": "private"},
    ])
    assert selected == [{
        "tool": "browser_forward",
        "state_ref": {"document_epoch": 11},
    }]
    assert owner._autonomous_resume_exact_call is None


def test_retained_exact_resume_metadata_is_internal_only():
    from osia import agent as agent_module

    owner = agent_module.OSIAAgent(lang="en")
    owner.messages = [
        {"role": "system", "content": "bounded"},
        {
            "role": "assistant",
            "content": "<tool_call>{}</tool_call>",
            "_provisional": True,
            "_autonomous_resume_call": {
                "tool": "upload_browser_file",
                "local_path": "private-value",
            },
        },
    ]
    wire = owner._wire_messages()
    assert "_provisional" not in wire[-1]
    assert "_autonomous_resume_call" not in wire[-1]
    assert agent_module._sanitize_persisted_history_message(
        owner.messages[-1]
    ) is None


def test_replan_reuses_verified_operations_by_stable_requirement(monkeypatch):
    from osia.autonomous import state as autonomous_state
    from osia.autonomous.orchestrator import Orchestrator

    run = autonomous_state.new_task_state(
        "1. Paginate the controlled fixture.",
        "deepseek",
        "model",
        [{"name": "Recovery", "tasks": [{
            "id": "replan.0",
            "requirement_id": "req_001",
            "objective": "Recover the verified pagination requirement.",
            "success_criterion": (
                "Use navigate_browser then paginate_browser and verify the "
                "next page."
            ),
        }]}],
    )
    run["capability_summary"] = [{"tool_category": "browser"}]
    run["operation_records"] = [
        {
            "operation_id": "op_old_navigation",
            "task_id": "task:old.4",
            "requirement_id": "req_001",
            "tool_identity": "navigate_browser",
            "status": "success_verified",
        },
        {
            "operation_id": "op_old_pagination",
            "task_id": "task:old.4",
            "requirement_id": "req_001",
            "tool_identity": "paginate_browser",
            "status": "success_verified",
            "browser_evidence": {
                "receipt": {
                    "dispatch_state": "effect_confirmed",
                    "outcome": "confirmed_success",
                    "evidence": [{"pagination_step": 1}],
                },
                "data": {"pages_advanced": 1},
            },
        },
        {
            "operation_id": "op_unrelated",
            "task_id": "task:old.5",
            "requirement_id": "req_002",
            "tool_identity": "search_web",
            "status": "success_verified",
        },
    ]
    task = run["phases"][0]["tasks"][0]
    orchestrator = Orchestrator(SimpleNamespace())
    orchestrator._state = run
    orchestrator._active_task = task
    monkeypatch.setattr(
        orchestrator,
        "_llm_judge_outcome",
        lambda _criterion, _evidence="": SimpleNamespace(
            ok=True, reason_code="verification_completed", category=None,
        ),
    )

    assert orchestrator._verified_operations_for_active_task() == [
        "navigate_browser", "paginate_browser",
    ]
    assert orchestrator._verify_outcome(task, "verified receipt evidence").ok
    verification_context = json.loads(
        orchestrator._browser_verification_context()
    )
    assert verification_context[
        "verified_operation_receipts_for_current_task"
    ][0]["operation"] == "paginate_browser"
    assert verification_context[
        "verified_operation_receipts_for_current_task"
    ][0]["typed_evidence"]["data"] == {"pages_advanced": 1}
    monkeypatch.setattr(autonomous_state, "save_task_state", lambda _state: None)
    assert orchestrator._step(run) is True
    assert task["status"] == "done"
    assert run["log"][-1]["event"] == "salvaged"
    assert run["log"][-1]["note"] == "verified_operation_receipt_reuse"


def test_named_structured_operation_cannot_be_verified_from_prose(monkeypatch):
    from osia.autonomous import state as autonomous_state
    from osia.autonomous.orchestrator import Orchestrator

    run = autonomous_state.new_task_state(
        "1. Use search_web for the controlled query.",
        "deepseek",
        "model",
        [{"name": "Search", "tasks": [{
            "id": "0.0",
            "requirement_id": "req_001",
            "objective": "Search",
            "success_criterion": "Use search_web for the controlled query.",
        }]}],
    )
    run["capability_summary"] = [{"tool_category": "browser"}]
    task = run["phases"][0]["tasks"][0]
    orchestrator = Orchestrator(SimpleNamespace())
    orchestrator._state = run
    orchestrator._active_task = task
    missing = orchestrator._verify_outcome(
        task, "I claim the search completed.",
    )
    assert missing.ok is False
    assert missing.reason_code == "required_browser_operation_missing"

    run["operation_records"].append({
        "task_id": "task:0.0",
        "tool_identity": "search_web",
        "status": "success_verified",
    })
    monkeypatch.setattr(
        orchestrator,
        "_llm_judge_outcome",
        lambda _criterion, _evidence="": SimpleNamespace(
            ok=True, reason_code="verification_completed", category=None,
        ),
    )
    assert orchestrator._verify_outcome(task, "Verified search result.").ok is True


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


def test_server_progress_projection_is_sanitized_and_terminal_is_canonical(monkeypatch):
    from osia.autonomous import state as autonomous_state
    from osia.server import server as server_module

    run = autonomous_state.new_task_state("private objective", "provider", "model", [])
    run["run_id"] = "wor186-terminal-run"
    run["browser_checkpoint"] = {
        "last_action_receipt": {
            "schema_version": 1,
            "operation_id": "operation_ref",
            "tool_call_id": "tool_call_ref",
            "before_state": None,
            "after_state": None,
            "dispatch_state": "effect_confirmed",
            "outcome": "confirmed_success",
            "evidence": [{
                "safe_url_after": (
                    "https://example.test/safe?token=QUERY_SENTINEL#fragment"
                ),
            }],
            "started_at": 1,
            "completed_at": 2,
        },
    }
    with server_module._AUTONOMOUS_PROTOCOL_LOCK:
        server_module._AUTONOMOUS_PROTOCOL_STATE["sequences"].pop(run["run_id"], None)
        server_module._AUTONOMOUS_PROTOCOL_STATE["terminals"].pop(run["run_id"], None)
    running = server_module._autonomous_frame({"event": "running"}, run)
    assert running["safe_site_display"] == "https://example.test"
    assert "private objective" not in json.dumps(running)
    unknown = server_module._autonomous_frame(
        {"event": "WOR186_PRIVATE_EVENT"}, run,
    )
    assert unknown["event"] == "state_updated"
    assert "WOR186_PRIVATE_EVENT" not in json.dumps(unknown)
    run["status"] = "done"
    run["run_state"] = "completed"
    terminal = server_module._autonomous_frame({"event": "done"}, run)
    repeated_terminal = server_module._autonomous_frame({"event": "failed"}, run)
    late = server_module._autonomous_frame({"event": "running"}, run)
    assert terminal["terminal"] is True
    assert repeated_terminal == terminal
    assert late == {"_drop_after_terminal": True, "run_id": run["run_id"]}

    monkeypatch.setattr(
        browser,
        "_browser_owner_snapshot",
        lambda: {
            "activeUrl": "https://example.test/path?token=QUERY_SENTINEL#fragment",
            "browserState": {"browser_session_id": "browser_safe_ref"},
        },
    )
    browser_frame = server_module._browser_progress_frame({"phase": "downloading"})
    assert browser_frame["safe_site_display"] == "https://example.test"
    assert "QUERY_SENTINEL" not in json.dumps(browser_frame)


def test_autonomous_browser_checkpoint_resume_and_suspension_rules(monkeypatch):
    from osia.autonomous import state as autonomous_state
    from osia.autonomous.orchestrator import Orchestrator

    tracker = BrowserStateTracker()
    tracker.start_context(object())
    state_ref = tracker.register_page(object())
    popup_ref = tracker.register_page(object(), popup=True)
    receipt = make_receipt(
        before_state=state_ref,
        after_state=state_ref,
        dispatch_state=DispatchState.EFFECT_CONFIRMED,
        outcome=ReceiptOutcome.CONFIRMED_SUCCESS,
        evidence=[{"download_finalized": True}],
    )
    run = autonomous_state.new_task_state("fixture", "provider", "model", [])
    autonomous_state.record_browser_execution(
        run,
        "download_browser_file",
        action_result(
            ok=True,
            receipt=receipt,
            data={
                "artifact": {
                    "artifact_id": "web_" + "a" * 32,
                    "size": 12,
                    "mime_type": "text/plain",
                    "classification": "opaque_content",
                },
            },
        ),
    )
    checkpoint = autonomous_state.normalize_browser_checkpoint(
        run["browser_checkpoint"]
    )
    checkpoint["active_popup_state"] = popup_ref.to_dict()
    run["browser_checkpoint"] = checkpoint
    assert checkpoint["browser_state"] == state_ref.to_dict()
    assert checkpoint["last_verified_state"] == state_ref.to_dict()
    assert checkpoint["last_artifact_summary"] == {
        "artifact_id": "web_" + "a" * 32,
        "size": 12,
        "mime_type": "text/plain",
        "classification": "opaque_content",
        "private": True,
    }

    orchestrator = Orchestrator(SimpleNamespace())
    monkeypatch.setattr(
        browser, "get_browser_state",
        lambda: {"ok": True, "data": {"state": state_ref.to_dict()}},
    )
    assert orchestrator._validate_browser_resume(run) is None
    autonomous_state.mark_pending_browser_operation(run, "download_ref", "download")
    assert orchestrator._validate_browser_resume(run) == "download_attention_required"
    run["browser_checkpoint"]["pending_operation"] = None
    monkeypatch.setattr(
        browser, "get_browser_state",
        lambda: {"ok": True, "data": {"state": popup_ref.to_dict()}},
    )
    assert orchestrator._validate_browser_resume(run) == "browser_state_stale"
    for reason in (
        "manual_login_required", "captcha_required", "two_factor_required",
    ):
        autonomous_state.suspend_browser(run, reason)
        assert run["run_state"] == "suspended_browser_intervention"
        assert run["browser_checkpoint"]["manual_intervention"] == reason


def test_current_document_opaque_references_survive_task_reset_only_until_stale():
    from osia.autonomous import state as autonomous_state
    from osia.autonomous.orchestrator import Orchestrator

    tracker = BrowserStateTracker()
    tracker.start_context(object())
    page = object()
    before = tracker.register_page(page)
    search_state = tracker.note_navigation(page)
    result_ref = "el_" + "a" * 32
    run = autonomous_state.new_task_state(
        "1. Search.\n2. Open the result.",
        "deepseek",
        "model",
        [{"name": "Web", "tasks": [{
            "id": "0.0",
            "requirement_id": "req_001",
            "objective": "Search",
            "success_criterion": "One result exists",
        }, {
            "id": "0.1",
            "requirement_id": "req_002",
            "objective": "Open the result",
            "success_criterion": "The result opens",
        }]}],
    )
    autonomous_state.record_browser_execution(
        run,
        "search_web",
        action_result(
            ok=True,
            receipt=make_receipt(
                before_state=before,
                after_state=search_state,
                dispatch_state=DispatchState.EFFECT_CONFIRMED,
                outcome=ReceiptOutcome.CONFIRMED_SUCCESS,
            ),
            data={
                "state": search_state.to_dict(),
                "results": [{
                    "result_ref": result_ref,
                    "title": "WOR186_PRIVATE_PAGE_SENTINEL",
                    "safe_destination": "https://fixture.invalid/private",
                }],
            },
        ),
    )
    # A later read-only inspection on the same document must not erase the
    # opaque result reference needed by the next independently planned task.
    autonomous_state.record_browser_execution(
        run,
        "inspect_browser_page",
        action_result(
            ok=True,
            receipt=make_receipt(
                before_state=search_state,
                after_state=search_state,
                dispatch_state=DispatchState.EFFECT_CONFIRMED,
                outcome=ReceiptOutcome.CONFIRMED_SUCCESS,
            ),
            data={"state": search_state.to_dict(), "title": "private title"},
        ),
    )
    handoffs = run["browser_checkpoint"]["reference_handoffs"]
    assert handoffs == [{
        "operation": "search_web",
        "state": search_state.to_dict(),
        "references": [{"field": "result_ref", "ref": result_ref}],
    }]
    orchestrator = Orchestrator(SimpleNamespace())
    orchestrator._state = run
    prompt = orchestrator._compose(run["phases"][0]["tasks"][1], "")
    assert result_ref in prompt
    assert "WOR186_PRIVATE_PAGE_SENTINEL" not in prompt
    assert "private title" not in prompt

    next_state = tracker.note_navigation(page)
    autonomous_state.record_browser_execution(
        run,
        "navigate_browser",
        action_result(
            ok=True,
            receipt=make_receipt(
                before_state=search_state,
                after_state=next_state,
                dispatch_state=DispatchState.EFFECT_CONFIRMED,
                outcome=ReceiptOutcome.CONFIRMED_SUCCESS,
            ),
            data={"state": next_state.to_dict()},
        ),
    )
    assert run["browser_checkpoint"]["reference_handoffs"] == []


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
    assert ambiguous_projection["must_not_replay"] is True
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
    ))

    state = _navigate(web.base + "/dialogs", state)
    dialog_page = _success(browser.inspect_browser_page(state))
    for index, action, prompt_text in (
        (1, "dismiss", None),
        (2, "accept", PRIVATE_SENTINEL),
    ):
        _success(browser.activate_browser_element(
            state, dialog_page["buttons"][index]["element_ref"],
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


def test_real_upload_authority_ask_exact_grant_and_full_access(
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

    policy = PermissionPolicy(PermissionStore(
        tmp_path / "permissions.dat", backend=_HermeticProtectedBackend(),
    ))
    previous_provider = mcp_registry.get_confirmation_provider()
    action_policy.set_default_permission_policy(policy)
    approvals = []
    try:
        mcp_registry.set_confirmation_provider(
            lambda *_args: approvals.append("ask") or True
        )
        ask_arguments = dict(base_arguments)
        assert action_policy.guard_tool_call(
            "Upload this exact synthetic file to the controlled form.",
            "upload_browser_file", ask_arguments,
        ) is None
        assert approvals == ["ask"]
        action_policy.clear_pending_execution_permit()

        request = build_permission_request(
            "upload_browser_file", {**base_arguments, **prepared},
            force_sensitive=True, policy=policy,
        )
        policy.store.add_grant(create_grant(
            request, use_policy=UsePolicy.BOUNDED, max_uses=1,
        ))
        mcp_registry.set_confirmation_provider(
            lambda *_args: pytest.fail("exact upload grant must avoid ASK")
        )
        assert action_policy.guard_tool_call(
            "Upload this exact synthetic file to the controlled form.",
            "upload_browser_file", dict(base_arguments),
        ) is None
        action_policy.clear_pending_execution_permit()

        policy.store.revoke_all_grants()
        policy.store.activate_full_access(
            activation_proof=issue_full_access_activation_proof(),
        )
        assert action_policy.guard_tool_call(
            "Upload this exact synthetic file to the controlled form.",
            "upload_browser_file", dict(base_arguments),
        ) is None
    finally:
        action_policy.clear_pending_execution_permit()
        action_policy.set_default_permission_policy(None)
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
    ))
    deadline = time.monotonic() + 2
    while web.duplicate_count < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert web.duplicate_count == 1

    state = _navigate(web.base + "/ambiguous")
    page = _success(browser.inspect_browser_page(state))
    ambiguous = browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
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
    ))
    assert popup["state"]["page_id"] != state["page_id"]
    opener = _success(browser.return_to_browser_opener(popup["state"]))["state"]
    assert opener["page_id"] == state["page_id"]
    _success(browser.close_browser_tab(popup["state"]))

    state = _navigate(web.base + "/dialogs", opener)
    page = _success(browser.inspect_browser_page(state))
    _success(browser.activate_browser_element(
        state, page["buttons"][0]["element_ref"],
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
    assert artifact["sha256"] == web.download_sha256
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
