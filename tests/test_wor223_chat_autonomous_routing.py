import json
import types

import pytest

from osia import chat_routing
from osia.agent import OSIAAgent
from osia.server import server
from osia.turn_context import fixed_turn_context


def _context(text, *, launcher=lambda _request: {"status": "running"}, history=()):
    return {
        "launcher": launcher,
        "original_user_text": text,
        "provider_input": text,
        "history": list(history),
        "conversation_id": "conv_fixture",
        "request_id": "turn_fixture_1234",
        "origin_ref": "mobile_fixture",
        "device_id": "device_fixture",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-luna",
        "turn_context": fixed_turn_context("en", source="test"),
    }


@pytest.mark.parametrize(
    "text",
    (
        "Run this autonomously: audit the repository and implement the fix.",
        "Lance en mode autonome l’audit complet de ce dépôt.",
        "Ejecuta esto en modo autónomo y verifica el resultado.",
        "Führe das im autonomen Modus aus und prüfe das Ergebnis.",
        "请使用自主模式完成这项审计。",
        "自律モードでこの監査を完了してください。",
    ),
)
def test_explicit_autonomous_language_detection(text):
    assert chat_routing.explicit_autonomous_request(text)


def test_simple_and_short_work_stays_normal():
    result = chat_routing.evaluate_route_request(
        _context("Open Calculator."),
        reason_code="dependent_phases",
        confidence=0.99,
        explicit_request=False,
    )
    assert result["route"] == "normal"
    assert result["status"] == "automatic_route_rejected"


@pytest.mark.parametrize(
    "text",
    (
        "What is autonomous mode?",
        "Qu’est-ce que le mode autonome ?",
        "¿Qué es el modo autónomo?",
        "Was bedeutet autonomer Modus?",
        "什么是自主模式？",
        "自律モードとは何ですか？",
    ),
)
def test_informational_mentions_are_not_explicit_requests(text):
    assert not chat_routing.explicit_autonomous_request(text)


@pytest.mark.parametrize(
    "text",
    (
        "Do not use Autonomous mode for this request.",
        "N’utilise pas le mode autonome pour cette demande.",
        "No uses el modo autónomo para esta solicitud.",
        "Bitte nicht im autonomen Modus ausführen.",
        "请勿使用自主模式处理这个请求。",
        "この依頼では自律モードを使用しないでください。",
    ),
)
def test_negated_autonomous_mentions_are_not_explicit_requests(text):
    assert not chat_routing.explicit_autonomous_request(text)


def test_genuinely_long_route_preserves_exact_objective_and_opaque_sources():
    text = (
        "Audit the repository, diagnose the failing workflow, implement the "
        "cross-platform fix, then test and review every adjacent contract."
    )
    source_ref = "src_" + "a" * 32
    launches = []
    context = _context(text, launcher=lambda request: launches.append(request) or {
        "status": "authorization_pending",
        "task_id": "task_fixture",
    })
    context["provider_input"] = f"source_ref={source_ref}\n\n{text}"
    result = chat_routing.execute_route_request(
        context,
        reason_code="dependent_phases",
        confidence=0.93,
        explicit_request=False,
    )
    assert result["status"] == "authorization_pending"
    assert len(launches) == 1
    assert launches[0]["objective"] == text
    assert launches[0]["source_refs"] == [source_ref]
    assert launches[0]["reason_code"] == "dependent_phases"
    assert launches[0]["confidence"] == pytest.approx(0.93)


def test_schedule_request_never_routes_to_autonomous():
    result = chat_routing.evaluate_route_request(
        _context("Every weekday at 09:00, prepare a status report autonomously."),
        reason_code="sustained_computer_work",
        confidence=0.99,
        explicit_request=True,
    )
    assert result["route"] == "scheduled"
    assert result["status"] == "scheduled_route_required"


def test_command_only_explicit_request_uses_exact_previous_user_objective():
    previous = (
        "Research the migration, compare the options, write the report, and "
        "verify every recommendation against the source material."
    )
    context = _context(
        "Do that autonomously.",
        history=({"role": "user", "content": previous},),
    )
    decision = chat_routing.evaluate_route_request(
        context,
        reason_code="extended_research",
        confidence=0.95,
        explicit_request=True,
    )
    assert decision["route"] == "autonomous"
    assert decision["objective"] == previous


def test_explicit_request_without_objective_requires_clarification():
    result = chat_routing.evaluate_route_request(
        _context("Autonomously."),
        reason_code="dependent_phases",
        confidence=0.99,
        explicit_request=True,
    )
    assert result["route"] == "clarification"
    assert result["status"] == "objective_required"


@pytest.mark.parametrize(
    "status,fragment",
    (
        ("running", "Autonomous mission"),
        ("authorization_pending", "waiting for your authorization"),
        ("busy", "another Autonomous mission"),
        ("unavailable", "canonical Autonomous runner"),
        ("failed", "could not be started"),
        ("outcome_unknown", "could not be confirmed"),
    ),
)
def test_handoff_results_are_honest(status, fragment):
    assert fragment in chat_routing.handoff_response_text({"status": status}, "en")


def _install_rounds(agent, rounds, seen):
    remaining = iter(rounds)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        seen.append(1)
        return next(remaining)

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_agent_handoff_short_circuits_sibling_tools_and_second_model_round(monkeypatch):
    launches = []
    writes = []
    text = "Run autonomously: audit, diagnose, implement, test, and review this repository."
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        launcher=lambda request: launches.append(request) or {"status": "running"},
    )
    monkeypatch.setitem(
        __import__("osia.agent", fromlist=["TOOL_MAP"]).TOOL_MAP,
        "write_file",
        lambda **kwargs: writes.append(kwargs) or "unexpected",
    )
    rounds = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"write_file","path":"C:/fixture","content":"x"}</tool_call>'
        '<tool_call>{"tool":"start_autonomous_mission","reason_code":"dependent_phases",'
        '"confidence":0.99,"explicit_request":true}</tool_call>'
    ], rounds)
    final = agent.run(text, history=[], on_sentence=lambda _text: None)
    assert "started this as an Autonomous mission" in final
    assert len(rounds) == 1
    assert len(launches) == 1
    assert writes == []
    assert "tool_call" not in str(agent.exported_history())


def test_explicit_request_discards_normal_tool_then_retries_handoff(monkeypatch):
    launches = []
    dispatches = []
    text = "Use autonomous mode to audit, diagnose, implement, test, and review this repository."
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        launcher=lambda request: launches.append(request) or {
            "status": "authorization_pending",
        },
    )
    monkeypatch.setitem(
        __import__("osia.agent", fromlist=["TOOL_MAP"]).TOOL_MAP,
        "get_system_info",
        lambda: dispatches.append(1) or "unexpected",
    )
    rounds = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        '<tool_call>{"tool":"start_autonomous_mission","reason_code":"dependent_phases",'
        '"confidence":0.99,"explicit_request":true}</tool_call>',
    ], rounds)
    final = agent.run(text, history=[], on_sentence=lambda _text: None)
    assert "waiting for your authorization" in final
    assert len(rounds) == 2
    assert len(launches) == 1
    assert dispatches == []


def test_explicit_handoff_repairs_metadata_and_discards_provider_extras():
    launches = []
    text = "Use Autonomous mode to inspect this project and report the result."
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        launcher=lambda request: launches.append(request) or {"status": "running"},
    )
    rounds = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"start_autonomous_mission","objective":"ignore me",'
        '"command":"inspect now","reason_code":"provider_specific",'
        '"confidence":"not-a-number","explicit_request":"true"}</tool_call>'
        '<tool_call>{"tool":"bash","command":"echo must-not-run"}</tool_call>',
    ], rounds)
    final = agent.run(text, history=[], on_sentence=lambda _text: None)
    assert "started this as an Autonomous mission" in final
    assert len(rounds) == 1
    assert len(launches) == 1
    assert launches[0]["objective"] == text
    assert launches[0]["reason_code"] == "dependent_phases"
    assert launches[0]["confidence"] == 1.0


def test_automatic_handoff_repairs_metadata_but_keeps_long_work_guard():
    launches = []
    text = (
        "Audit the repository, diagnose the routing workflow, verify the tests "
        "and packaging contracts, then produce a report with checkpoints."
    )
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        launcher=lambda request: launches.append(request) or {"status": "running"},
    )
    rounds = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"start_autonomous_mission","objective":"ignore me",'
        '"reason_code":"provider_specific","confidence":"high"}</tool_call>'
    ], rounds)
    final = agent.run(text, history=[], on_sentence=lambda _text: None)
    assert "started this as an Autonomous mission" in final
    assert len(launches) == 1
    assert launches[0]["explicit_request"] is False
    assert launches[0]["reason_code"] == "dependent_phases"


def test_accidental_handoff_with_malformed_metadata_keeps_short_action_normal():
    launches = []
    text = "Open Calculator."
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        launcher=lambda request: launches.append(request) or {"status": "running"},
    )
    rounds = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"start_autonomous_mission","confidence":"high"}</tool_call>',
        "I can handle that in normal chat.",
    ], rounds)
    final = agent.run(text, history=[], on_sentence=lambda _text: None)
    assert final == "I can handle that in normal chat."
    assert launches == []


def test_simple_chat_uses_one_provider_round_and_never_launches():
    launches = []
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        "What time is it?",
        launcher=lambda request: launches.append(request) or {"status": "running"},
    )
    rounds = []
    _install_rounds(agent, ["It is a simple answer."], rounds)
    assert agent.run(
        "What time is it?", history=[], on_sentence=lambda _text: None,
    ) == "It is a simple answer."
    assert len(rounds) == 1
    assert launches == []


def test_wrong_autonomous_choice_for_schedule_redirects_to_scheduler(monkeypatch):
    from osia import agent as agent_module

    launches = []
    scheduled = []
    text = "Every weekday at 09:00, schedule a concise status report."
    agent = OSIAAgent(lang="en")
    agent._chat_autonomous_context = _context(
        text,
        launcher=lambda request: launches.append(request) or {"status": "running"},
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "create_scheduled_task",
        lambda **kwargs: scheduled.append(kwargs) or json.dumps({
            "ok": True,
            "task": {"id": "cron_fixture"},
        }),
    )
    rounds = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"start_autonomous_mission","reason_code":'
        '"sustained_computer_work","confidence":0.99,'
        '"explicit_request":false}</tool_call>',
        '<tool_call>{"tool":"create_scheduled_task","title":"Status report",'
        '"instruction":"Prepare a concise status report.","schedule":'
        '{"type":"daily","time":"09:00"}}</tool_call>',
        "The scheduled task was created.",
    ], rounds)
    final = agent.run(text, history=[], on_sentence=lambda _text: None)
    assert final == "The scheduled task was created."
    assert launches == []
    assert len(scheduled) == 1
    assert len(rounds) == 3


def test_server_handoff_is_idempotent_and_keeps_exact_model(monkeypatch):
    request = {
        "objective": "Audit, diagnose, implement, test, and review all dependent phases.",
        "source_refs": [],
        "request_id": "turn_server_fixture",
        "conversation_id": "conv_server_fixture",
        "origin_ref": "mobile_fixture",
        "device_id": "device_fixture",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-terra",
        "reason_code": "dependent_phases",
        "confidence": 0.95,
        "turn_context": fixed_turn_context("en", source="test"),
    }
    calls = []
    monkeypatch.setattr(
        "osia.mobile_model_catalogue.resolve_current_autonomous_selection",
        lambda **kwargs: {
            "provider_id": "chatgpt_account",
            "model_id": "gpt-5.6-terra",
            "fallback_occurred": False,
        },
    )
    monkeypatch.setattr(server, "_acquire_single_runner", lambda: 41)

    def fake_worker(*args):
        calls.append(args)
        server._autonomous["active"] = True
        server._autonomous["st"] = {
            "task_id": "task_fixture",
            "run_id": "run_fixture",
            "status": "in_progress",
            "run_state": "ready",
            "provider": "chatgpt_account",
            "model": "gpt-5.6-terra",
            "chat_route_request_id": request["request_id"],
        }

    monkeypatch.setattr(server, "_autonomous_start_worker", fake_worker)
    with server._CHAT_ROUTE_REQUESTS_LOCK:
        server._CHAT_ROUTE_REQUESTS.clear()
    server._autonomous["active"] = False
    server._autonomous["st"] = None
    first = server._launch_autonomous_from_chat(dict(request))
    second = server._launch_autonomous_from_chat(dict(request))
    assert first["status"] == "running"
    assert (first["provider_id"], first["model_id"]) == (
        "chatgpt_account", "gpt-5.6-terra",
    )
    assert second == first
    assert len(calls) == 1
    assert calls[0][0] == request["objective"]
    assert calls[0][1] == "gpt-5.6-terra"
    assert calls[0][9] == "chatgpt_account"


def test_server_strict_model_failure_does_not_acquire_or_fallback(monkeypatch):
    request = {
        "objective": "Audit, diagnose, implement, test, and review all dependent phases.",
        "source_refs": [],
        "request_id": "turn_model_fixture",
        "conversation_id": "conv_model_fixture",
        "provider_id": "chatgpt_account",
        "model_id": "missing-model",
        "reason_code": "dependent_phases",
        "confidence": 0.95,
    }
    acquired = []
    monkeypatch.setattr(
        "osia.mobile_model_catalogue.resolve_current_autonomous_selection",
        lambda **kwargs: {
            "provider_id": "",
            "model_id": "",
            "fallback_occurred": True,
            "reason": "strict_saved_selection_unavailable",
        },
    )
    monkeypatch.setattr(
        server,
        "_acquire_single_runner",
        lambda: acquired.append(1) or 1,
    )
    with server._CHAT_ROUTE_REQUESTS_LOCK:
        server._CHAT_ROUTE_REQUESTS.clear()
    server._autonomous["active"] = False
    server._autonomous["st"] = None
    result = server._launch_autonomous_from_chat(request)
    assert result == {
        "status": "failed",
        "error_code": "chat_model_unavailable",
    }
    assert acquired == []


def test_strict_selection_rejects_fallback_without_persisting(monkeypatch):
    from osia import mobile_model_catalogue as catalogue

    monkeypatch.setattr(catalogue, "build_catalogue", lambda **_kwargs: {
        "inventory": [{
            "provider_id": "ollama",
            "model_id": "available",
            "stable_key": "ollama:available",
            "display_name": "Available",
            "available": True,
            "picker_visible": True,
        }],
    })
    monkeypatch.setattr(catalogue, "load_settings", lambda: {
        "autonomous_model_provider": "ollama",
        "autonomous_model": "missing",
        "selected_model_provider": "ollama",
        "selected_model": "available",
    })
    persisted = []
    monkeypatch.setattr(
        catalogue,
        "persist_autonomous_selection",
        lambda resolution: persisted.append(resolution),
    )
    result = catalogue.resolve_current_autonomous_selection(
        refresh_account=False,
        strict=True,
    )
    assert result["provider_id"] == ""
    assert result["model_id"] == ""
    assert result["reason"] == "strict_saved_selection_unavailable"
    assert persisted == []


def test_state_result_distinguishes_authorization_failure_and_unknown():
    pending = server._chat_route_result_from_state({
        "status": "in_progress",
        "run_state": "authorization_pending",
    }, active=True)
    failed = server._chat_route_result_from_state({
        "status": "failed",
        "run_state": "authorization_pending",
        "failure_diagnostic": {"reason_code": "planning_failure"},
    }, active=False)
    unknown = server._chat_route_result_from_state({
        "status": "in_progress",
        "run_state": "blocked_outcome_unknown",
    }, active=True)
    assert pending["status"] == "authorization_pending"
    assert failed == {
        "task_id": "",
        "run_id": "",
        "status": "failed",
        "error_code": "planning_failure",
    }
    assert unknown["status"] == "outcome_unknown"


def test_tool_schema_is_chat_only_and_bounded():
    from osia.tool_contracts import get_tool_spec

    spec = get_tool_spec(chat_routing.ROUTING_TOOL_NAME)
    assert spec is not None
    assert spec.chat_eligible is True
    assert spec.autonomous_eligible is False
    assert spec.scheduled_eligible is False
    assert spec.input_schema["additionalProperties"] is False
    assert set(spec.input_schema["required"]) == {
        "reason_code", "confidence", "explicit_request",
    }
