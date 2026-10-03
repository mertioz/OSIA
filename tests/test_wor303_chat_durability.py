"""WOR-303 absolute closure contract.

No arbitrary number may limit a feasible OSIA objective or an otherwise safe
tool use.  These tests are deliberately structural where possible: they prove
that no count/cycle/retry/threshold authority exists in the production runtime,
then prove the behavioral contract through the real controller, the real
strategy engine and the real provider loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from osia.agent import (
    OSIAAgent,
    TOOL_MAP,
    _AntiLoopGuard,
    _PROVIDER_FACT_BLOCKER_CODES,
    _provider_terminal_fact,
)
from osia.turn_terminality import TurnTerminalityController


_ROOT = Path(__file__).resolve().parents[1]

_FORBIDDEN_AUTHORITY = (
    # progress / no-progress terminality
    "_MAX_NO_PROGRESS_CYCLES",
    "_MAX_PROVIDER_GUIDANCE_WITHOUT_ATTEMPT",
    "_FORENSICS_AFTER_NO_PROGRESS_CYCLES",
    "provider_cannot_progress_no_usable_path",
    "provider_unavailable_no_usable_path",
    "_MAX_TRAILING_PROVIDER_FAILURES",
    "_trailing_provider_failures",
    "no_progress_repeat_count",
    "no_progress_fingerprint",
    "no_progress_history",
    "progress_revision",
    "_record_useful_progress",
    "useful_progress_count",
    "_finish_anti_loop_stop",
    "_TurnBudgetStop",
    "TURN_TIME_LIMIT_SECONDS",
    "TURN_HARD_LIMIT_SECONDS",
    "DEFAULT_TURN_HARD_LIMIT_SECONDS",
    "DEFAULT_TURN_IDLE_LIMIT_SECONDS",
    "remaining_provider_budget",
    "guard.check_time",
    # numeric repetition / retry / attempt authorities
    "_MAX_RECONCILIATION_GUIDANCE",
    "_reconciliation_guidance_count",
    "_MAX_IDENTICAL_ACTION_ATTEMPTS",
    "identical_action_retry_budget_exhausted",
    "signature_attempt_counts",
    "REPEAT_CALL_LIMIT",
    "STALL_CYCLE_REPEATS",
    "STALL_MAX_CYCLE_LEN",
    "refuse_repeat",
    "identical_call_replay_refused",
)

_V2_FORBIDDEN = (
    "max_model_calls",
    "boundary_stalls",
    "bounded_boundary_no_progress",
    "model_call_budget_exhausted",
    "bootstrap_retries",
    "remaining_refreshes",
)


def _controller(tmp_path, turn_id="wor303", frontier=("safe-strategy",)):
    controller = TurnTerminalityController(
        turn_id=turn_id,
        objective="finish the requested work",
        required_postconditions=("done",),
        execution_required=True,
        storage_dir=tmp_path,
    )
    controller.set_strategy_frontier(frontier)
    return controller


def test_runtime_sources_contain_no_numeric_objective_authority():
    for name in ("agent.py", "turn_terminality.py", "execution_strategy.py"):
        source = (_ROOT / "osia" / name).read_text(encoding="utf-8")
        for symbol in _FORBIDDEN_AUTHORITY:
            assert symbol not in source, (name, symbol)
    v2_source = (
        _ROOT / "osia" / "semantic_execution_loop_v2.py"
    ).read_text(encoding="utf-8")
    for symbol in _V2_FORBIDDEN:
        assert symbol not in v2_source, symbol


def test_no_max_execution_ceilings_remain():
    terminality = (
        _ROOT / "osia" / "turn_terminality.py"
    ).read_text(encoding="utf-8")
    assert re.findall(r"^_MAX_[A-Z0-9_]+", terminality, re.M) == []
    strategy = (
        _ROOT / "osia" / "execution_strategy.py"
    ).read_text(encoding="utf-8")
    assert "_MAX_PROGRESS_ATTEMPTS_PER_DESKTOP_MECHANISM" not in strategy
    assert "_MAX_IDENTICAL_ACTION_ATTEMPTS" not in strategy


def test_anti_loop_guard_is_a_factual_log_without_authority():
    guard = _AntiLoopGuard(lang="en")
    for attribute in (
        "check_time", "tripped", "stop_reason", "stop_instruction",
        "fallback_summary", "remaining_provider_budget",
        "remaining_hard_budget", "remaining_idle_budget",
        "useful_progress_count", "_record_useful_progress",
        "refuse_repeat", "refusals", "calls",
    ):
        assert not hasattr(guard, attribute), attribute
    for _ in range(20):
        guard.record("read_file", {"path": "a"}, "content", False)
    # Twenty identical safe calls are logged, never refused.
    assert len(guard.actions) == 20
    assert all(ok for _name, ok in guard.actions)


def test_ten_consecutive_provider_failures_do_not_terminalize(tmp_path):
    controller = _controller(tmp_path, "wor303-provider-failures")
    for index in range(10):
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id="safe-strategy",
            postcondition="done",
        )
        controller.record_provider_invocation_completed(invocation)
        controller.record_typed_evidence(
            outcome="failed_before_effect",
            evidence={"error": "ProviderError: transport", "invocation_id": invocation},
            reason_code="provider_invocation_failed_before_effect",
        )
    decision = controller.evaluate(provider_route="p:m")
    assert decision.terminal_state == ""
    assert decision.action in {"continue", "wait"}
    assert "provider_unavailable" not in decision.reason_code
    # The objective is still alive and can still complete after the failures.
    controller.prove_postcondition("done", {"verified": True})
    assert controller.evaluate().terminal_state == "success_verified"


def test_arbitrary_provider_failure_count_never_creates_a_threshold(tmp_path):
    controller = _controller(tmp_path, "wor303-provider-failures-long")
    for index in range(100):
        controller.record_typed_evidence(
            outcome="failed_before_effect",
            evidence={"error": "ProviderError", "invocation_id": f"inv-{index}"},
            reason_code="provider_timeout",
        )
        decision = controller.evaluate(provider_route="p:m")
        assert decision.terminal_state == "", index
        assert not controller.reserve_final(
            "blocked_verified",
            reason_code="provider_unavailable_no_usable_path",
        )
    assert controller.snapshot()["strategy_frontier"] == ["safe-strategy"]


def test_local_tool_timeout_does_not_final_the_objective(tmp_path):
    controller = _controller(tmp_path, "wor303-tool-timeout")
    controller.record_typed_evidence(
        outcome="failed_before_effect",
        evidence={"error": "tool timeout", "invocation_id": "inv-timeout"},
        reason_code="tool_timeout",
    )
    for _ in range(5):
        decision = controller.evaluate(provider_route="p:m")
        assert decision.action == "continue"
        assert decision.terminal_state == ""
    controller.prove_postcondition("done", {"verified": True})
    assert controller.evaluate().terminal_state == "success_verified"


def test_factual_permission_state_carries_no_terminal_authority(tmp_path):
    # WOR-301 removed the OSIA permission system.  The legacy permission field
    # is an inert diagnostic and must never block, wait or terminalize a turn.
    controller = _controller(tmp_path, "wor303-permission")
    controller.set_permission_state("user_denied")
    decision = controller.evaluate(provider_route="p:m")
    assert decision.terminal_state == ""
    assert decision.action == "continue"
    controller.set_permission_state("pending")
    decision = controller.evaluate(provider_route="p:m")
    assert decision.terminal_state == ""
    assert decision.action == "continue"
    source = (
        _ROOT / "osia" / "turn_terminality.py"
    ).read_text(encoding="utf-8")
    assert "permission_user_denied" not in source
    assert "_PROVEN_PERMISSION_BLOCKERS" not in source


class _ProviderAPIError(Exception):
    def __init__(self, status, body=None, message=""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.body = body or {}


def _api_error(status, *, code="", message="", error_type=""):
    return _ProviderAPIError(status, body={
        "error": {
            "code": code,
            "type": error_type,
            "message": message or f"HTTP {status}",
        },
    })


def test_provider_fact_classification_is_error_derived_not_count_derived():
    agent = OSIAAgent(lang="en")
    agent.provider = "fixture-provider"
    agent.model = "fixture-model"
    # Retryable / local failures classify as no terminal fact.
    assert _provider_terminal_fact(TimeoutError("read timed out")) == ""
    assert _provider_terminal_fact(ConnectionError("connection reset")) == ""
    assert _provider_terminal_fact(_api_error(500, message="server error")) == ""
    assert _provider_terminal_fact(_api_error(
        429, message="Rate limit reached for requests",
        error_type="rate_limit_exceeded",
    )) == ""
    assert _provider_terminal_fact(_api_error(
        429, message="Too many requests", code="rate_limit_exceeded",
    )) == ""
    # Explicit non-retryable provider facts classify precisely.
    assert _provider_terminal_fact(_api_error(
        401, message="Invalid API key",
    )) == "provider_credentials_invalid"
    assert _provider_terminal_fact(_api_error(
        403, message="The model `gpt-x` is not accessible for this account",
        code="model_not_accessible",
    )) == "provider_model_inaccessible"
    assert _provider_terminal_fact(_api_error(
        404, message="The model `gpt-x` does not exist",
        code="model_not_found",
    )) == "provider_model_not_found"
    assert _provider_terminal_fact(_api_error(
        429, message="You exceeded your current quota",
        error_type="insufficient_quota",
    )) == "provider_hard_quota_exhausted"
    assert _provider_terminal_fact(_api_error(
        402, message="Payment required for this account",
    )) == "provider_billing_restricted"
    assert _provider_terminal_fact(_api_error(
        400, message="This model does not support tools",
    )) == "provider_contract_incompatible"
    assert _provider_terminal_fact(Exception("unknown provider 'x'")) == (
        "provider_not_configured"
    )
    # Classification is pure: it never mutates provider/model authority.
    assert (agent.provider, agent.model) == ("fixture-provider", "fixture-model")


def test_explicit_provider_fact_reserves_precise_blocker(tmp_path):
    controller = _controller(tmp_path, "wor303-provider-fact")
    controller.record_typed_evidence(
        outcome="failed_before_effect",
        evidence={"error": "AuthenticationError: invalid api key"},
        reason_code="provider_credentials_invalid",
    )
    controller.mark_provider_fact_blocker("provider_credentials_invalid")
    decision = controller.evaluate(provider_route="p:m")
    assert decision.terminal_state == "blocked_verified"
    assert decision.reason_code == "provider_credentials_invalid"
    assert (
        controller.snapshot()["provider_fact_blocker"]
        == "provider_credentials_invalid"
    )


def test_provider_fact_survives_arbitrary_transient_noise(tmp_path):
    controller = _controller(tmp_path, "wor303-provider-fact-noise")
    for index in range(50):
        controller.record_typed_evidence(
            outcome="failed_before_effect",
            evidence={"error": "timeout", "invocation_id": f"inv-{index}"},
            reason_code="provider_timeout",
        )
        assert controller.evaluate(provider_route="p:m").terminal_state == ""
    controller.mark_provider_fact_blocker("provider_model_not_found")
    decision = controller.evaluate(provider_route="p:m")
    assert decision.terminal_state == "blocked_verified"
    assert decision.reason_code == "provider_model_not_found"


def test_one_hundred_transient_failures_then_recovery_succeeds(tmp_path):
    controller = _controller(tmp_path, "wor303-provider-recovery")
    for index in range(100):
        controller.record_typed_evidence(
            outcome="failed_before_effect",
            evidence={"error": "ProviderError", "invocation_id": f"inv-{index}"},
            reason_code="provider_timeout",
        )
        decision = controller.evaluate(provider_route="p:m")
        assert decision.terminal_state == "", index
        assert not controller.reserve_final(
            "blocked_verified",
            reason_code="provider_unavailable_no_usable_path",
        )
    invocation = controller.begin_invocation(
        provider_route="p:m",
        strategy_id="safe-strategy",
        postcondition="done",
    )
    controller.record_provider_invocation_completed(invocation)
    controller.prove_postcondition("done", {"verified": True})
    assert controller.evaluate().terminal_state == "success_verified"


def test_unresolved_effect_does_not_terminalize_from_call_count(tmp_path):
    controller = _controller(tmp_path, "wor303-reconcile")
    controller.record_typed_evidence(
        outcome="outcome_unknown",
        evidence={"dispatch": "possible"},
        attempt_id="attempt-unknown",
    )
    # WOR-305 keeps the uncertain receipt factual and permits continuation;
    # it does not require a legacy strategy label or exhaust a call counter.
    for _ in range(5):
        decision = controller.evaluate(provider_route="p:m")
        assert decision.action == "continue"
        assert decision.terminal_state == ""
        assert "attempt-unknown" in controller.snapshot()["unknown_effects"]
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=decision.next_strategy or "safe-strategy",
            postcondition=decision.postcondition,
        )
        controller.record_provider_invocation_completed(invocation)
    # Factual resolution (fresh read-only evidence) clears the unknown effect.
    controller.reconcile_unknown(
        "attempt-unknown", outcome="success_verified",
        evidence={"read_only_verification": "effect_present"},
    )
    assert controller.snapshot()["unknown_effects"] == {}
    decision = controller.evaluate(provider_route="p:m")
    assert decision.terminal_state == ""


def test_objective_continues_across_many_provider_segments_without_cap(tmp_path):
    controller = _controller(tmp_path, "wor303-many")
    invocation_ids = []
    for _ in range(50):
        decision = controller.evaluate(provider_route="p:m")
        assert decision.action == "continue"
        assert decision.terminal_state == ""
        assert "NO_PROGRESS" not in decision.reason_code
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=decision.next_strategy,
            postcondition=decision.postcondition,
        )
        invocation_ids.append(invocation)
        controller.record_provider_invocation_completed(invocation)
    assert len(set(invocation_ids)) == 50
    snapshot = controller.snapshot()
    assert snapshot["strategy_frontier"] == ["safe-strategy"]
    assert snapshot["terminal_state"] == ""
    controller.prove_postcondition("done", {"verified": True})
    assert controller.evaluate().terminal_state == "success_verified"


def test_prose_only_segments_never_terminalize_from_a_counter(tmp_path):
    controller = _controller(tmp_path, "wor303-prose")
    for _ in range(40):
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id="safe-strategy",
            postcondition="done",
        )
        controller.record_provider_invocation_completed(
            invocation, native_status="completed",
            response_fingerprint="same-prose-every-time",
        )
    decision = controller.evaluate(provider_route="p:m")
    assert decision.action == "continue"
    assert decision.terminal_state == ""
    assert decision.next_strategy == "safe-strategy"
    assert not controller.reserve_final(
        "blocked_verified",
        reason_code="provider_cannot_progress_no_usable_path",
    )
    assert controller.snapshot()["strategy_frontier"] == ["safe-strategy"]


def test_replan_instruction_is_never_a_terminal_budget(tmp_path):
    controller = _controller(tmp_path, "wor303-replan", frontier=())
    for _ in range(30):
        decision = controller.evaluate(provider_route="p:m")
        assert decision.action == "continue"
        assert decision.next_strategy == "provider_replan_required"
        assert decision.terminal_state == ""
    controller.prove_postcondition("done", {"verified": True})
    assert controller.evaluate().terminal_state == "success_verified"


def test_partial_work_stays_eligible_for_continuation(tmp_path):
    controller = _controller(tmp_path, "wor303-partial")
    controller.record_typed_evidence(
        outcome="success_unverified",
        evidence={"partial": True},
    )
    for _ in range(5):
        decision = controller.evaluate(provider_route="p:m")
        assert decision.terminal_state == ""
        assert controller.snapshot()["remaining_work"] == ["done"]
        if decision.action != "continue":
            # Partial verified work may require read-only reconciliation before
            # more mutations; it is still the same active objective.
            assert decision.action == "wait"
            break
        invocation = controller.begin_invocation(
            provider_route="p:m",
            strategy_id=decision.next_strategy,
            postcondition=decision.postcondition,
        )
        controller.record_provider_invocation_completed(invocation)
    controller.prove_postcondition("done", {"verified": True})
    assert controller.evaluate().terminal_state == "success_verified"


class _RepeatingCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        message = SimpleNamespace(content=self.responses[index], tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_RepeatingCompletions(responses))


def _tool_call(tool, **kwargs):
    payload = {"tool": tool, **kwargs}
    return "<tool_call>\n" + json.dumps(payload) + "\n</tool_call>"


def _run_agent_turn(monkeypatch, request, responses):
    agent = OSIAAgent(lang="en")
    agent.provider = "ollama"
    agent.model = "wor303-model"
    agent.client = _FakeClient(responses)
    monkeypatch.setattr(agent, "_resolve_route", lambda _text: ("medium", []))
    return agent, agent.run(request, history=[])


def test_twenty_identical_safe_read_calls_are_not_refused(monkeypatch):
    from osia import agent as agent_module

    executed = []
    monkeypatch.setitem(
        TOOL_MAP,
        "read_file",
        lambda **kwargs: executed.append(kwargs) or "file-content",
    )
    responses = [_tool_call("read_file", path="C:/Temp/a.txt")] * 20
    responses.append("Read it twenty times.")
    _run_agent_turn(
        monkeypatch, "Read C:/Temp/a.txt repeatedly as a stress loop.", responses,
    )
    assert len(executed) == 20


def test_alternating_safe_tool_cycle_is_not_refused(monkeypatch):
    from osia import agent as agent_module

    executed = []
    monkeypatch.setitem(
        TOOL_MAP, "list_files",
        lambda **kwargs: executed.append("list_files") or "a.txt",
    )
    monkeypatch.setitem(
        TOOL_MAP, "get_current_url",
        lambda **kwargs: executed.append("get_current_url") or "https://a.test/",
    )
    cycle = [
        _tool_call("list_files", path="C:/Temp"),
        _tool_call("get_current_url"),
    ]
    responses = cycle * 10
    responses.append("Cycle completed.")
    _run_agent_turn(
        monkeypatch, "Alternate listing and URL checks as a stress loop.", responses,
    )
    assert executed.count("list_files") == 10
    assert executed.count("get_current_url") == 10


def test_identical_safe_action_is_not_blocked_by_attempt_count():
    from osia.execution_strategy import (
        execution_strategy_scope, tool_preflight,
    )

    action = {"path": "C:/Temp/report.txt", "content": "x"}
    with execution_strategy_scope(
        "Create the report file C:/Temp/report.txt", {"write_file"},
    ) as strategy:
        first = tool_preflight("write_file", dict(action))
        assert first.allowed is True
        strategy.record(first, "failed", failure_reason="fixture_failed")
        stale = tool_preflight("write_file", dict(action))
        assert stale.allowed is True
        assert stale.reason_code == "strategy_selected"
        # Fresh evidence re-enables the same safe action; the former
        # two-attempt count gate would have refused here regardless of evidence.
        strategy.observe("state changed", evidence_token="revision-1")
        second = tool_preflight("write_file", dict(action))
        assert second.allowed is True
        strategy.record(second, "failed", failure_reason="fixture_failed")
        strategy.observe("state changed again", evidence_token="revision-2")
        third = tool_preflight("write_file", dict(action))
        assert third.allowed is True


class _ScriptedCompletions:
    def __init__(self, responses, on_exhausted=None):
        self.responses = list(responses)
        self.calls = 0
        self.on_exhausted = on_exhausted

    def create(self, **kwargs):
        if self.calls >= len(self.responses) and self.on_exhausted is not None:
            callback, self.on_exhausted = self.on_exhausted, None
            callback()
        index = min(self.calls, len(self.responses) - 1)
        content = self.responses[index]
        self.calls += 1
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_invalid_credentials_block_with_precise_fact(tmp_path):
    import osia.turn_terminality as terminality_module

    with patch.object(terminality_module, "CONFIG_DIR", str(tmp_path)):
        agent = OSIAAgent(model="wor303-model", lang="en")
        agent.provider = "fixture-provider"

        def fail_auth(_text, **_kwargs):
            raise _ProviderAPIError(401, message="Invalid API key")

        agent.run = fail_auth
        final = agent.run_terminal_turn(
            "Do the requested work",
            turn_id="wor303-credentials",
            required_postconditions=("work done",),
            execution_required=True,
        )
        result = agent.terminality_result("wor303-credentials") or {}
        assert result["terminal_state"] == "blocked_verified"
        assert result["reason_code"] == "provider_credentials_invalid"
        assert "credential" in final.lower() or "401" in final
        # Provider/model authority is never silently switched by the blocker.
        assert agent.provider == "fixture-provider"
        assert agent.model == "wor303-model"


def _persisted_snapshot(root, turn_id):
    path = (
        Path(root) / "turn_terminality"
        / (hashlib.sha256(turn_id.encode("utf-8")).hexdigest() + ".json")
    )
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def test_follow_up_turn_starts_clean_after_selected_model_final(tmp_path):
    agent = OSIAAgent(model="wor303-model", lang="en")
    agent.provider = "ollama"
    agent._resolve_route = lambda _text: ("medium", [])

    first_turn = "wor303-first"
    first = _ScriptedCompletions(
        [
            "I will start the long objective now.",
            "Still working on the long objective.",
        ],
        on_exhausted=lambda: agent.request_turn_cancellation(
            first_turn, effect_possible=False,
        ),
    )
    agent.client = _FakeClient([])
    agent.client.chat.completions = first
    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        agent.run_terminal_turn(
            "Build me a large deliverable from A to Z.",
            turn_id=first_turn,
            execution_required=True,
            current_conversation_id="wor303-conversation",
            canonical_output=True,
        )
    first_result = agent.terminality_result(first_turn) or {}
    assert first_result["terminal_state"] == "success_verified"
    first_snapshot = _persisted_snapshot(tmp_path, first_turn)
    assert first_snapshot["completion_text"] == first_result["final_text"]
    assert first_snapshot["typed_evidence"] == []
    assert first_snapshot["active_attempts"] == {}

    second_turn = "wor303-follow-up"
    second = _ScriptedCompletions([
        "Voici l'explication demandée."
        '<osia_completion>{"outcomes":[{"id":"explanation",'
        '"result":"the requested explanation","kind":"assistant_content",'
        '"receipt_ids":[]}]}</osia_completion>'
    ])
    agent.client = _FakeClient([])
    agent.client.chat.completions = second
    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        final = agent.run_terminal_turn(
            "explique en détails ce qui vient de se passer",
            turn_id=second_turn,
            execution_required=False,
            current_conversation_id="wor303-conversation",
            canonical_output=True,
        )
    assert final == "Voici l'explication demandée."
    second_snapshot = _persisted_snapshot(tmp_path, second_turn)
    assert second_snapshot["terminal_state"] == "success_verified"
    # No GoalContract/frontier/typed evidence from the previous turn is active.
    assert second_snapshot["strategy_frontier"] == []
    assert second_snapshot["remaining_work"] == []
    assert second_snapshot["typed_evidence"] == []
    first_snapshot = _persisted_snapshot(tmp_path, first_turn)
    assert first_snapshot["turn_id"] == first_turn
    assert second_snapshot["turn_id"] == second_turn
