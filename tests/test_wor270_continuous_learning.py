"""WOR-270's evidence-only learning store and canonical Chat projection."""

from __future__ import annotations

import copy
import json
import sys
import time
from types import SimpleNamespace

import pytest

from osia import agent as agent_module
from osia.continuous_learning import (
    LearningEngine,
    LearningStore,
    LearningStoreError,
    observation_from_tool_attempt,
)
from osia.memory import MemoryStore
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore


def _terminal_attempt(
    store: ToolAttemptStore,
    suffix: str,
    *,
    outcome: str = "success",
    domain: str = "developer.mozilla.org",
    application: str = "",
    tool_name: str = "browser_search",
    reason_code: str = "fixture_terminal_evidence",
) -> str:
    target = {"kind": "browser"}
    if domain:
        target["safe_url"] = f"https://{domain}/docs/private-path?token=not-retained"
    if application:
        target = {"kind": "desktop", "application": application}
    created = store.create_attempt(
        conversation_id=f"conv-{suffix}",
        turn_id=f"turn-{suffix}",
        request_id=f"request-{suffix}",
        tool_call_id=f"call-{suffix}",
        tool_name=tool_name,
        provider_id="fixture-provider",
        model_id="fixture-model",
        turn_context=None,
        argument_fingerprint=store.fingerprint_arguments({
            "private": "WOR270_PRIVATE_SENTINEL",
        }),
        side_effect="read_only",
        idempotence="idempotent",
        retry_policy="retry_read_only",
        owner_scope="chat",
        action_binding={"operation": tool_name, "target": target},
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    terminal = {
        "success": (
            ToolAttemptStatus.SUCCEEDED,
            "success_verified",
            "effect_confirmed",
        ),
        "failure": (
            ToolAttemptStatus.FAILED_BEFORE_EFFECT,
            "confirmed_failure_before_effect",
            "not_dispatched",
        ),
        "unknown": (
            ToolAttemptStatus.UNKNOWN_AFTER_START,
            "outcome_unknown",
            "effect_unknown",
        ),
    }[outcome]
    store.record_terminal_result(
        running["tool_attempt_id"],
        status=terminal[0],
        terminal_outcome=terminal[1],
        dispatch_state=terminal[2],
        reason_code=reason_code,
    )
    return created["tool_attempt_id"]


def _engine(tmp_path):
    learning = LearningStore(tmp_path / "learning" / "learning.json")
    attempts = ToolAttemptStore(tmp_path / "attempts")
    return LearningEngine(learning, attempts), learning, attempts


def _pattern(store: LearningStore, *, domain: str | None = None, app: str | None = None):
    patterns = store.inspect()["patterns"]
    for pattern in patterns:
        if domain and pattern["scope"].get("domain") == domain:
            return pattern
        if app and pattern["scope"].get("application") == app.casefold():
            return pattern
    raise AssertionError("expected learning pattern was not stored")


def test_verified_successes_accumulate_from_toolattempt_and_actiontruth(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    refs = [
        _terminal_attempt(attempts, "success-1"),
        _terminal_attempt(attempts, "success-2"),
    ]

    assert engine.ingest_terminal_attempts() == 2
    pattern = _pattern(learning, domain="developer.mozilla.org")
    assert pattern["verified_success_count"] == 2
    assert pattern["verified_failure_count"] == 0
    assert pattern["supporting_evidence_refs"] == sorted(refs)
    observation = learning.inspect()["observations"][0]
    assert observation["action_id"].startswith("action_")
    assert observation["effect_truth"] == "verified"
    assert observation["terminal_outcome"] == "success_verified"
    assert engine.ingest_terminal_attempts() == 0


def test_verified_failure_contradicts_prior_success_and_reduces_confidence(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "success-a")
    _terminal_attempt(attempts, "success-b")
    engine.ingest_terminal_attempts()
    before = _pattern(learning, domain="developer.mozilla.org")["confidence"]
    failure_ref = _terminal_attempt(attempts, "failure-a", outcome="failure")
    engine.ingest_terminal_attempts()

    pattern = _pattern(learning, domain="developer.mozilla.org")
    assert pattern["verified_success_count"] == 2
    assert pattern["verified_failure_count"] == 1
    assert failure_ref in pattern["contradicting_evidence_refs"]
    assert pattern["confidence"] < before


def test_outcome_unknown_remains_unknown_and_is_not_success(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "known-success")
    unknown_ref = _terminal_attempt(attempts, "unknown", outcome="unknown")
    engine.ingest_terminal_attempts()

    pattern = _pattern(learning, domain="developer.mozilla.org")
    assert pattern["verified_success_count"] == 1
    assert pattern["verified_failure_count"] == 0
    assert pattern["outcome_unknown_count"] == 1
    assert pattern["unknown_evidence_refs"] == [unknown_ref]
    assert engine.context_for_request(
        "Search developer.mozilla.org using Chrome",
    ) == ""


def test_application_and_domain_scopes_are_isolated(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    for suffix in ("mdn-1", "mdn-2"):
        _terminal_attempt(attempts, suffix, domain="developer.mozilla.org")
    for suffix in ("example-1", "example-2"):
        _terminal_attempt(attempts, suffix, domain="example.com")
    for suffix in ("chrome-1", "chrome-2"):
        _terminal_attempt(
            attempts, suffix, domain="", application="Chrome.exe",
            tool_name="desktop_click",
        )
    engine.ingest_terminal_attempts()

    mdn_context = engine.context_for_request("Search developer.mozilla.org")
    example_context = engine.context_for_request("Search example.com")
    chrome_context = engine.context_for_request("Click in Chrome")
    assert "developer.mozilla.org" in mdn_context
    assert "example.com" not in mdn_context
    assert "example.com" in example_context
    assert "chrome" in chrome_context
    assert "developer.mozilla.org" not in chrome_context


def test_one_sample_is_inspectable_but_not_surfaced_as_a_pattern(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "one-success")
    engine.ingest_terminal_attempts()

    assert _pattern(learning, domain="developer.mozilla.org")[
        "verified_success_count"
    ] == 1
    assert engine.context_for_request(
        "Search developer.mozilla.org",
    ) == ""


def test_learning_persistence_contains_no_raw_arguments_or_page_content(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "privacy")
    engine.ingest_terminal_attempts()
    engine.context_for_request(
        "Search developer.mozilla.org; private note WOR270_QUERY_SENTINEL",
    )

    saved = learning.path.read_text(encoding="utf-8")
    assert "WOR270_PRIVATE_SENTINEL" not in saved
    assert "WOR270_QUERY_SENTINEL" not in saved
    assert "/docs/private-path" not in saved
    assert "token=not-retained" not in saved
    assert "argument_fingerprint" not in saved


def test_relevant_jit_context_is_short_factual_and_provenance_aware(tmp_path):
    engine, _learning, attempts = _engine(tmp_path)
    refs = [
        _terminal_attempt(attempts, "jit-1"),
        _terminal_attempt(attempts, "jit-2"),
    ]
    engine.ingest_terminal_attempts()

    context = engine.context_for_request(
        "Search developer.mozilla.org for the current API guide",
    )
    assert "Historical execution evidence" in context
    assert "browser/search via browser_search" in context
    assert "2 verified successes" in context
    assert f"platform={sys.platform}" in context
    assert any(ref in context for ref in refs)
    assert len(context) <= 1_200
    assert "must use" not in context.casefold()


def test_irrelevant_operation_and_scope_receive_no_learning_context(tmp_path):
    engine, _learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "irrelevant-1")
    _terminal_attempt(attempts, "irrelevant-2")
    engine.ingest_terminal_attempts()

    assert engine.context_for_request("Write a note in developer.mozilla.org") == ""
    assert engine.context_for_request("Search example.com") == ""
    assert engine.context_for_request("What is the weather today?") == ""


def test_canonical_chat_injects_history_to_the_same_selected_model_only(tmp_path):
    engine, _learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "chat-history-1")
    _terminal_attempt(attempts, "chat-history-2")
    engine.ingest_terminal_attempts()
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            message = SimpleNamespace(content="Understood.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=message, finish_reason="stop",
            )])

    agent = agent_module.OSIAAgent(model="selected-test-model", lang="en")
    agent.provider = "ollama"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent._memory_store = MemoryStore(tmp_path / "memory.json")
    agent._learning_engine = engine
    agent.configure_tool_attempts(
        conversation_id="fixture-conversation",
        turn_id="fixture-turn",
        request_id="fixture-request",
        final_response_message_id="fixture-assistant",
    )
    available_names_before = set(agent_module.KNOWN_TOOL_NAMES)
    system_prompt_before = agent._system_prompt_for_current_turn()

    result = agent._run_turn(
        "Search developer.mozilla.org using Chrome",
        history=[],
        current_conversation_id="fixture-conversation",
    )

    assert result == "Understood."
    assert len(calls) == 1
    assert calls[0]["model"] == "selected-test-model"
    assert calls[0]["messages"][0]["content"] == system_prompt_before
    learning_messages = [
        message for message in calls[0]["messages"]
        if "Historical execution evidence" in str(message.get("content") or "")
    ]
    assert len(learning_messages) == 1
    assert learning_messages[0]["role"] == "user"
    assert not any(message.get("_learning_context") for message in calls[0]["messages"])
    assert set(agent_module.KNOWN_TOOL_NAMES) == available_names_before
    assert agent.provider == "ollama"
    assert agent.model == "selected-test-model"


def test_learning_message_does_not_leak_into_later_unrelated_turn(tmp_path):
    engine, _learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "history-filter-1")
    _terminal_attempt(attempts, "history-filter-2")
    engine.ingest_terminal_attempts()
    captured = []

    class Completions:
        def create(self, **kwargs):
            captured.append(copy.deepcopy(kwargs.get("messages") or []))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Okay.", tool_calls=None),
                finish_reason="stop",
            )])

    agent = agent_module.OSIAAgent(model="selected-test-model", lang="en")
    agent.provider = "ollama"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent._memory_store = MemoryStore(tmp_path / "memory.json")
    agent._learning_engine = engine
    agent.configure_tool_attempts(
        conversation_id="fixture-conversation",
        turn_id="fixture-turn-one",
        request_id="fixture-request-one",
        final_response_message_id="fixture-assistant-one",
    )
    agent._run_turn("Search developer.mozilla.org", history=[])
    agent.configure_tool_attempts(
        conversation_id="fixture-conversation",
        turn_id="fixture-turn-two",
        request_id="fixture-request-two",
        final_response_message_id="fixture-assistant-two",
    )
    agent._run_turn("Tell me a joke.", history=None)

    assert any(
        "Historical execution evidence" in str(message.get("content") or "")
        for message in captured[0]
    )
    assert not any(
        "Historical execution evidence" in str(message.get("content") or "")
        for message in captured[1]
    )


def test_explicit_correction_is_scoped_and_stays_distinct_from_tool_failure(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "correction-base-1")
    _terminal_attempt(attempts, "correction-base-2")
    engine.ingest_terminal_attempts()
    pattern = _pattern(learning, domain="developer.mozilla.org")
    correction_id = learning.record_user_correction(
        scope={"platform": sys.platform, "domain": "developer.mozilla.org"},
        tool_family="browser",
        operation_family="search",
        approach="browser_search",
        evidence_ref="turn-explicit-correction",
        correction_code="result_was_incorrect",
    )

    updated = _pattern(learning, domain="developer.mozilla.org")
    assert updated["verified_failure_count"] == 0
    assert updated["user_correction_count"] == 1
    assert "turn-explicit-correction" in updated["contradicting_evidence_refs"]
    assert correction_id in {
        item["observation_id"] for item in learning.inspect()["observations"]
    }
    context = engine.context_for_request("Search developer.mozilla.org")
    assert "1 explicit user corrections" in context
    assert pattern["pattern_id"] == updated["pattern_id"]


def test_recency_and_contradictions_weaken_descriptive_confidence(tmp_path):
    _engine_obj, learning, _attempts = _engine(tmp_path)
    now = time.time()
    base = {
        "kind": "tool_attempt",
        "action_id": "action_fixture",
        "semantic_effect_id": "effect_fixture",
        "truth_state": "effect_verified",
        "effect_truth": "verified",
        "attempt_status": "succeeded",
        "terminal_outcome": "success_verified",
        "scope": {"platform": sys.platform, "domain": "developer.mozilla.org"},
        "tool_family": "browser",
        "operation_family": "search",
        "approach": "browser_search",
        "latency_ms": 100,
        "correction_code": "",
    }
    old_success = {
        **base,
        "observation_id": "obs_old_success",
        "evidence_ref": "attempt-old-success",
        "outcome": "success_verified",
        "occurred_at": now - 365 * 24 * 60 * 60,
    }
    fresh_success = {
        **base,
        "observation_id": "obs_fresh_success",
        "evidence_ref": "attempt-fresh-success",
        "outcome": "success_verified",
        "occurred_at": now,
    }
    fresh_failure = {
        **base,
        "observation_id": "obs_fresh_failure",
        "evidence_ref": "attempt-fresh-failure",
        "truth_state": "failed_before_effect",
        "effect_truth": "not_possible",
        "attempt_status": "failed_before_effect",
        "terminal_outcome": "confirmed_failure_before_effect",
        "outcome": "failure_verified",
        "occurred_at": now,
    }
    learning.add_observations([old_success, fresh_success])
    before = learning.inspect()["patterns"][0]["confidence"]
    learning.add_observations([fresh_failure])
    after = learning.inspect()["patterns"][0]["confidence"]

    assert before > 0.65
    assert after < before
    assert after < 0.6


def test_scope_delete_and_full_reset_preserve_unrelated_attempt_journal(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "delete-mdn-1", domain="developer.mozilla.org")
    _terminal_attempt(attempts, "delete-mdn-2", domain="developer.mozilla.org")
    _terminal_attempt(attempts, "keep-example-1", domain="example.com")
    _terminal_attempt(attempts, "keep-example-2", domain="example.com")
    engine.ingest_terminal_attempts()
    before_attempts = list(attempts.attempts_dir.glob("attempt_*.json"))

    assert learning.delete_scope({"domain": "developer.mozilla.org"}) == 2
    assert [item["scope"].get("domain") for item in learning.inspect()["patterns"]] == [
        "example.com",
    ]
    assert len(list(attempts.attempts_dir.glob("attempt_*.json"))) == len(before_attempts)
    learning.reset()
    assert learning.inspect()["observations"] == []
    assert learning.inspect()["patterns"] == []


def test_restart_persistence_inspection_ignore_and_reset_are_local(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    ref1 = _terminal_attempt(attempts, "persist-1")
    ref2 = _terminal_attempt(attempts, "persist-2")
    engine.ingest_terminal_attempts()
    pattern = _pattern(learning, domain="developer.mozilla.org")

    reopened = LearningStore(learning.path)
    assert ref1 in reopened.inspect()["patterns"][0]["supporting_evidence_refs"]
    assert ref2 in reopened.inspect()["patterns"][0]["supporting_evidence_refs"]
    reopened.ignore_pattern(pattern["pattern_id"])
    assert reopened.context_for_request("Search developer.mozilla.org") == ""
    reopened.ignore_pattern(pattern["pattern_id"], ignored=False)
    assert reopened.context_for_request("Search developer.mozilla.org")
    reopened.reset()
    assert reopened.inspect()["observation_count"] == 0
    assert len(list(attempts.attempts_dir.glob("attempt_*.json"))) == 2


def test_skill_promotion_produces_review_only_candidate_after_independent_evidence(tmp_path):
    _engine_obj, learning, _attempts = _engine(tmp_path)
    observations = []
    for index, domain in enumerate((
        "developer.mozilla.org", "example.com", "python.org",
        "developer.mozilla.org", "example.com", "python.org",
        "developer.mozilla.org", "example.com",
    )):
        observations.append({
            "observation_id": f"obs_candidate_{index}",
            "kind": "tool_attempt",
            "evidence_ref": f"attempt-candidate-{index}",
            "action_id": f"action-candidate-{index}",
            "semantic_effect_id": f"effect-candidate-{index}",
            "truth_state": "effect_verified",
            "effect_truth": "verified",
            "attempt_status": "succeeded",
            "terminal_outcome": "success_verified",
            "scope": {"platform": sys.platform, "domain": domain},
            "tool_family": "browser",
            "operation_family": "search",
            "approach": "browser_search",
            "outcome": "success_verified",
            "occurred_at": 2_000_000_000.0 + index,
            "latency_ms": 250,
            "correction_code": "",
        })
    learning.add_observations(observations)
    pattern = _pattern(learning, domain="developer.mozilla.org")
    candidate = learning.propose_skill_candidate(pattern["pattern_id"])

    assert candidate is not None
    assert candidate["status"] == "review_required"
    assert candidate["scope_count"] == 3
    assert candidate["runtime_activation"] is False
    assert candidate["promotion_requires_explicit_review"] is True
    assert learning.propose_skill_candidate("pattern_missing") is None


def test_learning_disabled_and_corrupt_state_fail_open_to_canonical_chat(tmp_path):
    learning_path = tmp_path / "learning" / "learning.json"
    learning_path.parent.mkdir(parents=True)
    learning_path.write_text("{broken", encoding="utf-8")
    learning = LearningStore(learning_path)
    engine = LearningEngine(learning, ToolAttemptStore(tmp_path / "attempts"))
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Still operational.", tool_calls=None),
                finish_reason="stop",
            )])

    agent = agent_module.OSIAAgent(model="selected-test-model", lang="en")
    agent.provider = "ollama"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    agent._memory_store = MemoryStore(tmp_path / "memory.json")
    agent._learning_engine = engine
    agent.configure_tool_attempts(
        conversation_id="fixture-conversation",
        turn_id="fixture-corrupt-turn",
        request_id="fixture-corrupt-request",
        final_response_message_id="fixture-corrupt-assistant",
    )
    assert agent._run_turn("Search developer.mozilla.org", history=[]) == "Still operational."
    assert len(calls) == 1
    assert not any(
        "Historical execution evidence" in str(message.get("content") or "")
        for message in calls[0]["messages"]
    )

    with pytest.raises(LearningStoreError):
        learning.snapshot()


def test_disabled_learning_stops_ingestion_until_reenabled(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "disabled-1")
    _terminal_attempt(attempts, "disabled-2")
    learning.set_enabled(False)

    assert engine.ingest_terminal_attempts() == 0
    assert learning.inspect()["observation_count"] == 0
    assert engine.context_for_request("Search developer.mozilla.org") == ""

    learning.set_enabled(True)
    assert engine.ingest_terminal_attempts() == 2
    assert engine.context_for_request("Search developer.mozilla.org")


def test_observer_refuses_unverified_or_non_chat_attempts(tmp_path):
    _engine_obj, _learning, attempts = _engine(tmp_path)
    _terminal_attempt(attempts, "unknown-observation", outcome="unknown")
    raw = next(attempts.get_attempt(path.stem)
               for path in attempts.attempts_dir.glob("attempt_*.json"))
    projected = observation_from_tool_attempt(raw)
    assert projected is not None
    assert projected["outcome"] == "outcome_unknown"
    raw["owner_scope"] = "autonomous"
    assert observation_from_tool_attempt(raw) is None


def test_permission_and_policy_denials_do_not_seed_approach_failures(tmp_path):
    engine, learning, attempts = _engine(tmp_path)
    _terminal_attempt(
        attempts,
        "permission-denied",
        outcome="failure",
        reason_code="permission_user_denied",
    )

    assert engine.ingest_terminal_attempts() == 0
    assert learning.inspect()["observation_count"] == 0
