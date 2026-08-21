import threading
import time

import pytest

from osia import agent as agent_module
from osia import tool_attempts
from osia.agent import OSIAAgent
from osia.tool_attempts import (
    ToolAttemptCoordinator,
    ToolAttemptStatus,
    ToolAttemptStore,
)
from osia.tool_contracts import (
    ExecutionOutcome,
    ToolExecutionRecord,
    effective_contract,
    get_tool_spec,
    persistence_contract,
)
from osia.turn_context import fixed_turn_context


class FakeClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def _policy(family="generic", *, inactivity=10.0, hard=100.0, alternatives=()):
    return {
        "family": family,
        "release_after_seconds": 0.05,
        "inactivity_timeout_seconds": inactivity,
        "hard_timeout_seconds": hard,
        "check_delays_seconds": [1.0, 2.0, 4.0],
        "safe_alternatives": list(alternatives),
        "restart_reconciliation": True,
    }


def _running_attempt(
    store,
    *,
    side_effect="external_mutation",
    owner_scope="chat",
    policy=None,
):
    created = store.create_attempt(
        conversation_id=f"conv_{owner_scope}",
        turn_id=f"turn_{owner_scope}",
        request_id=f"request_{owner_scope}",
        tool_call_id=f"call_{owner_scope}",
        tool_name="fixture_tool",
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments({"opaque": True}),
        side_effect=side_effect,
        idempotence="unknown",
        retry_policy="never_after_dispatch",
        deadline_at=tool_attempts._now() + (policy or _policy())["hard_timeout_seconds"],
        owner_scope=owner_scope,
        persistence_contract=policy or _policy(),
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    return store.mark_running(accepted["tool_attempt_id"])


def _attempt_context(store, coordinator, conversation="conv_slow"):
    return {
        "enabled": True,
        "store": store,
        "conversation_id": conversation,
        "turn_id": "turn_slow",
        "request_id": "request_slow",
        "tool_call_id": "call_slow",
        "provider_id": "fixture-provider",
        "model_id": "fixture-model",
        "turn_context": fixed_turn_context(
            "en", source="fixture", reference="turn_slow",
        ),
        "final_response_message_id": "assistant_slow",
        "terminal_callback": coordinator.notify_terminal,
    }


def test_tool_families_resolve_distinct_persistence_contracts():
    cases = {
        "activate_browser_element": "browser",
        "read_file": "file",
        "get_system_info": "system",
        "gmail_list_labels": "api_mcp",
    }
    schedules = {}
    for name, expected_family in cases.items():
        spec = get_tool_spec(name)
        contract = persistence_contract(
            spec,
            tool_name=name,
            arguments={},
            owner_timeout_seconds=30.0,
        )
        assert contract.family.value == expected_family
        assert contract.release_after_seconds < contract.hard_timeout_seconds
        schedules[name] = contract.check_delays_seconds
    assert len(set(schedules.values())) == len(schedules)


def test_fast_success_returns_without_a_waiting_attempt(tmp_path, monkeypatch):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda **_kwargs: calls.append(1) or "fast verified success",
    )
    record = agent_module._execute_tool_with_policy(
        "write_file",
        {"path": "fixture.txt", "content": "fixture"},
        lang="en",
        attempt_context=_attempt_context(store, coordinator),
    )
    assert record.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    assert calls == [1]
    assert store.get_attempt(record.tool_attempt_id)["persistence_state"] == "terminal"


def test_slow_owner_releases_chat_and_late_success_is_verified_once(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    completed = threading.Event()
    calls = []

    def slow_success(**_kwargs):
        calls.append(1)
        time.sleep(0.2)
        completed.set()
        return "slow verified success"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", slow_success)
    original_timeout = agent_module._configured_tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_configured_tool_timeout_seconds",
        lambda spec, name, kwargs: (
            0.02 if name == "write_file" else original_timeout(spec, name, kwargs)
        ),
    )
    started = time.monotonic()
    record = agent_module._execute_tool_with_policy(
        "write_file",
        {"path": "fixture.txt", "content": "fixture"},
        lang="en",
        attempt_context=_attempt_context(store, coordinator),
    )
    elapsed = time.monotonic() - started
    assert record.outcome is ExecutionOutcome.PENDING
    assert elapsed < 0.15
    assert completed.wait(1.0)
    deadline = time.monotonic() + 1.0
    durable = store.get_attempt(record.tool_attempt_id)
    while durable["status"] != "succeeded" and time.monotonic() < deadline:
        time.sleep(0.01)
        durable = store.get_attempt(record.tool_attempt_id)
    assert durable["status"] == "succeeded"
    assert durable["reason_code"] == "owner_callback"
    assert calls == [1]


def test_late_unverified_owner_completion_enables_read_only_postcondition(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    calls = []

    def slow_unverified(**_kwargs):
        calls.append(1)
        time.sleep(0.08)
        return "browser action accepted"

    monkeypatch.setitem(
        agent_module.TOOL_MAP, "navigate_browser", slow_unverified,
    )
    original_timeout = agent_module._configured_tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_configured_tool_timeout_seconds",
        lambda spec, name, kwargs: (
            0.01
            if name == "navigate_browser"
            else original_timeout(spec, name, kwargs)
        ),
    )
    record = agent_module._execute_tool_with_policy(
        "navigate_browser",
        {
            "url": "https://example.invalid/fixture",
            "wait_spec": {
                "condition_type": "url_change",
                "timeout_seconds": 1.0,
            },
        },
        lang="en",
        attempt_context=_attempt_context(
            store, coordinator, conversation="conv_postcondition",
        ),
    )
    assert record.outcome is ExecutionOutcome.PENDING
    deadline = time.monotonic() + 1.0
    durable = store.get_attempt(record.tool_attempt_id)
    while (
        durable["dispatch_state"] != "owner_settled_unverified"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
        durable = store.get_attempt(record.tool_attempt_id)
    assert durable["status"] == "waiting_verification"
    assert durable["dispatch_state"] == "owner_settled_unverified"
    assert durable["reason_code"] == "postcondition_required_unverified"
    assert calls == [1]


def test_late_ambiguous_effect_waits_without_replay_or_hard_limit_extension(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    calls = []

    def slow_ambiguous(**_kwargs):
        calls.append(1)
        time.sleep(0.08)
        return "ERROR: remote outcome unavailable"

    monkeypatch.setitem(
        agent_module.TOOL_MAP, "navigate_browser", slow_ambiguous,
    )
    original_timeout = agent_module._configured_tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_configured_tool_timeout_seconds",
        lambda spec, name, kwargs: (
            0.01
            if name == "navigate_browser"
            else original_timeout(spec, name, kwargs)
        ),
    )
    record = agent_module._execute_tool_with_policy(
        "navigate_browser",
        {"url": "https://example.invalid/ambiguous"},
        lang="en",
        attempt_context=_attempt_context(
            store, coordinator, conversation="conv_ambiguous",
        ),
    )
    assert record.outcome is ExecutionOutcome.PENDING
    initial = store.get_attempt(record.tool_attempt_id)
    hard_deadline = initial["hard_deadline_at"]
    deadline = time.monotonic() + 1.0
    durable = initial
    while (
        durable["reason_code"] != "effect_possible_unverified"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
        durable = store.get_attempt(record.tool_attempt_id)
    assert durable["status"] == "waiting_verification"
    assert durable["dispatch_state"] == "owner_settled_unverified"
    assert durable["hard_deadline_at"] == hard_deadline
    assert calls == [1]


def test_unique_progress_extends_inactivity_but_not_hard_ceiling(
    tmp_path, monkeypatch,
):
    clock = FakeClock()
    monkeypatch.setattr(tool_attempts, "_now", clock)
    store = ToolAttemptStore(tmp_path / "attempts")
    running = _running_attempt(store, policy=_policy(inactivity=10.0, hard=40.0))
    hard = running["hard_deadline_at"]
    original_idle = running["inactivity_deadline_at"]

    clock.advance(8.0)
    first = store.record_progress(
        running["tool_attempt_id"],
        phase="downloading",
        useful=True,
        evidence_token="download-phase-1",
    )
    assert first["inactivity_deadline_at"] > original_idle
    assert first["hard_deadline_at"] == hard

    clock.advance(5.0)
    repeated = store.record_progress(
        running["tool_attempt_id"],
        phase="downloading",
        useful=True,
        evidence_token="download-phase-1",
    )
    assert repeated["inactivity_deadline_at"] == first["inactivity_deadline_at"]
    assert repeated["useful_progress_count"] == 1

    clock.advance(4.0)
    second = store.record_progress(
        running["tool_attempt_id"],
        phase="finalizing",
        useful=True,
        evidence_token="download-phase-2",
    )
    assert second["inactivity_deadline_at"] > repeated["inactivity_deadline_at"]
    assert second["hard_deadline_at"] == hard


def test_stagnation_terminates_with_typed_state_and_never_replays(
    tmp_path, monkeypatch,
):
    clock = FakeClock()
    monkeypatch.setattr(tool_attempts, "_now", clock)
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    running = _running_attempt(store, policy=_policy(inactivity=5.0, hard=30.0))
    store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=clock() + 1.0,
        deadline_at=running["hard_deadline_at"],
    )
    finalizations = []
    coordinator.register_suspended(
        running["tool_attempt_id"],
        resume=lambda *_args: "canonical final",
        finalize=lambda *_args: finalizations.append(1) or False,
        verify=lambda _attempt_id: None,
    )
    monkeypatch.setattr(coordinator, "_schedule_timer", lambda *_a, **_k: None)
    assert coordinator.activate(running["tool_attempt_id"])
    clock.advance(6.0)
    coordinator._timer_fired(running["tool_attempt_id"])
    terminal = store.get_attempt(running["tool_attempt_id"])
    assert terminal["status"] == "unknown_after_start"
    assert terminal["reason_code"] == "reconciliation_stalled"
    assert terminal["persistence_state"] == "stalled"
    assert terminal["terminal_state"] == "terminal"
    assert terminal["dispatch_state"] == "effect_unknown"


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        ("ERROR: DNS_PROBE_POSSIBLE getaddrinfo failed", "dns_failure"),
        ("ERROR: HTTP 401 unauthorized", "authentication_failure"),
        ("ERROR: HTTP 403 permission denied", "permission_failure"),
    ],
)
def test_terminal_external_errors_are_exact_and_not_retried(
    payload, reason_code, monkeypatch,
):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: calls.append(1) or payload,
    )
    record = agent_module._execute_tool_with_policy("get_system_info", {})
    assert record.outcome is ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert record.reason_code == reason_code
    assert record.attempts == 0
    assert record.dispatched is False
    assert calls == [1]


def test_blocked_command_is_proven_before_effect(monkeypatch):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "run_command",
        lambda **_kwargs: calls.append(1) or "ERROR: BLOCK_SYSTEM_DESTRUCTION",
    )
    record = agent_module._execute_tool_with_policy(
        "run_command", {"command": "Remove-Item protected"},
    )
    assert record.outcome is ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert record.reason_code == "command_blocked"
    assert record.dispatched is False
    assert calls == [1]


def test_fresh_browser_state_reresolves_delayed_postcondition_read_only(
    tmp_path, monkeypatch,
):
    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    policy = _policy(
        "browser",
        inactivity=20.0,
        hard=60.0,
        alternatives=("fresh_browser_postcondition",),
    )
    running = _running_attempt(store, policy=policy)
    waiting = store.mark_waiting_verification(
        running["tool_attempt_id"],
        next_check_at=time.time(),
        deadline_at=running["hard_deadline_at"],
        owner_settled=True,
    )
    spec = get_tool_spec("activate_browser_element")
    contract = effective_contract(spec, {})
    original = ToolExecutionRecord(
        "accepted", False, ExecutionOutcome.SUCCESS_UNVERIFIED,
        1, True, False, contract, waiting["tool_attempt_id"], False,
    )
    state = {
        "schema_version": 1,
        "browser_session_id": "browser_AAAAAAAAAAAAAAAAAAAAAAAA",
        "context_id": "context_AAAAAAAAAAAAAAAAAAAAAAAA",
        "context_epoch": 1,
        "page_id": "page_AAAAAAAAAAAAAAAAAAAAAAAA",
        "page_generation": 2,
        "navigation_id": "nav_BBBBBBBBBBBBBBBBBBBBBBBB",
        "document_epoch": 2,
        "created_at": 1.0,
    }
    from osia.tools import browser as browser_tools

    monkeypatch.setattr(
        browser_tools,
        "get_browser_state",
        lambda: {"data": {"state": state}},
    )
    monkeypatch.setattr(
        browser_tools,
        "wait_for_browser_condition",
        lambda state_ref, wait_spec: {
            "ok": True,
            "data": {"state": state_ref},
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
            },
        },
    )
    agent = object.__new__(OSIAAgent)
    agent._pending_tool_runtime = {
        "attempt_id": waiting["tool_attempt_id"],
        "wait_spec": {
            "condition_type": "url_change",
            "timeout_seconds": 30.0,
        },
        "record": original,
    }
    verified = agent._verify_suspended_tool(waiting["tool_attempt_id"])
    assert verified.outcome is ExecutionOutcome.SUCCESS_VERIFIED
    terminal = store.get_attempt(waiting["tool_attempt_id"])
    assert terminal["status"] == "succeeded"
    assert terminal["attempted_safe_alternatives"] == [
        "fresh_browser_postcondition"
    ]
    assert terminal["useful_progress_count"] == 1


def test_safe_alternative_registry_is_bounded_and_nonduplicating(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    running = _running_attempt(
        store,
        policy=_policy(
            "browser",
            alternatives=("fresh_browser_postcondition",),
        ),
    )
    attempt_id = running["tool_attempt_id"]
    assert store.record_safe_alternative(
        attempt_id, "fresh_browser_postcondition",
    ) is True
    assert store.record_safe_alternative(
        attempt_id, "fresh_browser_postcondition",
    ) is False
    assert store.record_safe_alternative(attempt_id, "unsafe_replay") is False
    assert store.get_attempt(attempt_id)["attempted_safe_alternatives"] == [
        "fresh_browser_postcondition"
    ]


def test_restart_keeps_future_attempt_reconcilable_and_resumes_once(
    tmp_path, monkeypatch,
):
    clock = FakeClock()
    monkeypatch.setattr(tool_attempts, "_now", clock)
    store = ToolAttemptStore(tmp_path / "attempts")
    first = ToolAttemptCoordinator(store)
    running = _running_attempt(store, policy=_policy(inactivity=20.0, hard=60.0))
    store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=clock() + 1.0,
        deadline_at=running["hard_deadline_at"],
    )
    assert first.startup_fail_closed(now=clock()) == []
    waiting = store.get_attempt(running["tool_attempt_id"])
    assert waiting["status"] == "waiting_verification"
    assert waiting["reason_code"] == "restart_reconciliation_pending"

    resumed = ToolAttemptCoordinator(store)
    finals = []
    resumed.register_suspended(
        running["tool_attempt_id"],
        resume=lambda *_args: "one final",
        finalize=lambda *_args: finals.append(1) or False,
    )
    monkeypatch.setattr(resumed, "_schedule_timer", lambda *_a, **_k: None)
    assert resumed.activate(running["tool_attempt_id"])
    terminal, created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
        reason_code="delayed_success_verified",
    )
    assert created is True
    resumed.notify_terminal(running["tool_attempt_id"], terminal)
    resumed.notify_terminal(running["tool_attempt_id"], terminal)
    deadline = time.monotonic() + 1.0
    while not finals and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finals == [1]


@pytest.mark.parametrize("owner_scope", ["chat", "autonomous"])
def test_chat_and_autonomous_share_one_canonical_terminal_callback(
    tmp_path, owner_scope,
):
    store = ToolAttemptStore(tmp_path / owner_scope)
    coordinator = ToolAttemptCoordinator(store)
    running = _running_attempt(store, owner_scope=owner_scope)
    finals = []
    coordinator.register_suspended(
        running["tool_attempt_id"],
        resume=lambda *_args: "canonical",
        finalize=lambda *_args: finals.append(owner_scope) or False,
    )
    terminal, _created = store.record_terminal_result(
        running["tool_attempt_id"],
        status=ToolAttemptStatus.SUCCEEDED,
        terminal_outcome="success_verified",
        dispatch_state="effect_confirmed",
    )
    coordinator.notify_terminal(running["tool_attempt_id"], terminal)
    coordinator.notify_terminal(running["tool_attempt_id"], terminal)
    assert coordinator.activate(running["tool_attempt_id"])
    deadline = time.monotonic() + 1.0
    while not finals and time.monotonic() < deadline:
        time.sleep(0.01)
    assert finals == [owner_scope]
