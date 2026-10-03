from __future__ import annotations

import threading
import time
import types

import pytest

from osia import action_policy
from osia import agent as agent_module
from osia.agent import OSIAAgent
from osia.mcp_client import registry as mcp_registry
from osia.tool_contracts import (
    ConfirmationPolicy,
    ConfirmationLedger,
    ExecutionOutcome,
    SideEffectClass,
    effective_contract,
    get_static_registry,
    shell_command_is_proven_read_only,
)


def test_representative_static_side_effect_and_confirmation_classes():
    registry = get_static_registry()
    for name in (
        "get_system_info", "read_file", "read_page", "gmail_read_message",
        "read_document_artifact",
    ):
        assert registry.get(name).side_effect == SideEffectClass.READ_ONLY
        assert registry.get(name).confirmation == ConfirmationPolicy.NO_CONFIRMATION
    for name in (
        "write_file", "save_memory", "create_scheduled_task", "delete_file",
    ):
        assert registry.get(name).side_effect == SideEffectClass.PERSISTENT_LOCAL
    assert registry.get("fill_form_field").side_effect == SideEffectClass.EXTERNAL_MUTATION
    assert registry.get("gmail_send_draft").side_effect == SideEffectClass.IRREVERSIBLE_EXTERNAL
    assert registry.get("run_command").side_effect == SideEffectClass.DYNAMICALLY_CLASSIFIED


def test_shell_classifier_is_narrow_and_ambiguous_syntax_is_high_risk():
    assert shell_command_is_proven_read_only("Get-Process") is True
    assert shell_command_is_proven_read_only("dir C:\\Temp") is True
    for command in (
        "echo ok", "dir | findstr txt", "dir > out.txt", "python script.py",
        "powershell -Command Get-Process", "npm install x", "dir; whoami",
        "$(whoami)",
    ):
        assert shell_command_is_proven_read_only(command) is False

    spec = get_static_registry().get("run_command")
    safe = effective_contract(spec, {"command": "Get-Process"})
    risky = effective_contract(spec, {"command": "echo WOR178_SECRET"})
    assert safe.side_effect == SideEffectClass.READ_ONLY
    assert risky.side_effect == SideEffectClass.UNKNOWN_HIGH_RISK


def test_confirmation_ledger_is_exact_expiring_and_single_use():
    now = [100.0]
    ledger = ConfirmationLedger(ttl_seconds=5.0, clock=lambda: now[0])
    grant = ledger.issue("delete_file", {"path": "opaque-a"})
    assert ledger.consume(grant.token, "delete_file", {"path": "opaque-b"}) is False

    grant = ledger.issue("delete_file", {"path": "opaque-a"})
    assert ledger.consume(grant.token, "delete_file", {"path": "opaque-a"}) is True
    assert ledger.consume(grant.token, "delete_file", {"path": "opaque-a"}) is False

    grant = ledger.issue("delete_file", {"path": "opaque-a"})
    now[0] += 6.0
    assert ledger.consume(grant.token, "delete_file", {"path": "opaque-a"}) is False


def test_contract_gate_dispatches_without_any_confirmation_prompt(monkeypatch):
    prompts = []
    monkeypatch.setattr(
        mcp_registry,
        "_confirmation_provider",
        lambda server, tool, summary: prompts.append((server, tool, summary)) or False,
    )
    secret = "WOR178_CONFIRM_SECRET"
    assert action_policy.guard_tool_call(
        "Run the requested local command.",
        "run_command",
        {"command": f"echo {secret}"},
    ) is None
    assert action_policy.guard_tool_call(
        "Read system information.", "get_system_info", {},
    ) is None
    assert prompts == []


def test_connector_writes_proceed_without_consulting_any_approver(monkeypatch):
    prompts = []
    monkeypatch.setattr(
        mcp_registry,
        "_confirmation_provider",
        lambda server, tool, summary: prompts.append((tool, summary)) or True,
    )
    assert mcp_registry.confirm_connector_write(
        "osia", "delete_file", "Delete one local item",
        call_arguments={"path": "opaque-a"},
    ) is None
    assert mcp_registry.confirm_connector_write(
        "osia", "delete_file", "Delete one local item",
        call_arguments={"path": "opaque-b"},
    ) is None
    assert prompts == []


def test_gmail_confirmation_summaries_never_include_private_values():
    from osia.tools import gmail

    secret = "WOR178_GMAIL_SECRET"
    summaries = [
        gmail._draft_confirmation_summary({
            "to": [f"{secret}@example.test"],
            "subject": secret,
            "body": secret,
        }),
        gmail._send_draft_confirmation_summary({
            "draftId": secret,
            "to": secret,
            "subject": secret,
            "snippet": secret,
        }),
        gmail._message_modify_confirmation_summary(
            secret, secret, add_labels=(secret,), remove_labels=(),
        ),
        gmail._label_modify_confirmation_summary(secret, secret, secret),
    ]
    assert all(secret not in summary for summary in summaries)


def test_read_failure_retries_once_but_effecting_results_never_retry(monkeypatch):
    read_calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: read_calls.append(1) or (
            "ERROR: transient read" if len(read_calls) == 1 else "ok"
        ),
    )
    read_record = agent_module._execute_tool_with_policy("get_system_info", {})
    assert read_record.attempts == 2
    assert read_record.outcome == ExecutionOutcome.SUCCESS_VERIFIED
    assert len(read_calls) == 2

    write_calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda path, content: write_calls.append((path, content)) or "",
    )
    write_record = agent_module._execute_tool_with_policy(
        "write_file", {"path": "opaque", "content": "private"},
    )
    assert write_record.attempts == 1
    assert write_record.outcome == ExecutionOutcome.OUTCOME_UNKNOWN
    assert len(write_calls) == 1


def test_expired_turn_deadline_proves_effecting_tool_never_started(monkeypatch):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda path, content: calls.append(1) or "unexpected",
    )
    record = agent_module._execute_tool_with_policy(
        "write_file",
        {"path": "opaque", "content": "private"},
        hard_deadline=time.monotonic() - 1.0,
    )
    assert record.outcome == ExecutionOutcome.NOT_STARTED
    assert record.dispatched is False
    assert record.attempts == 0
    assert calls == []


def test_tool_exception_redacts_arguments_results_and_internal_exception(monkeypatch, capsys):
    secret = "WOR178_EXCEPTION_SECRET"

    def failing():
        raise RuntimeError(secret)

    monkeypatch.setitem(agent_module.TOOL_MAP, "get_system_info", failing)
    record = agent_module._execute_tool_with_policy(
        "get_system_info", {}, allow_retry=False, lang="en",
    )
    captured = capsys.readouterr()
    assert record.outcome == ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
    assert secret not in str(record.result)
    assert secret not in captured.out
    assert secret not in captured.err


def test_private_tool_result_is_model_facing_but_not_logged_or_exported(monkeypatch, capsys):
    secret = "WOR178_RESULT_SECRET"
    monkeypatch.setitem(agent_module.TOOL_MAP, "read_file", lambda path: secret)
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="en")
    rounds = [0]

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        if rounds[0] == 0:
            rounds[0] += 1
            return '<tool_call>{"tool":"read_file","path":"opaque"}</tool_call>'
        assert any(secret in str(message.get("content")) for message in self.messages)
        rounds[0] += 1
        return "Read completed."

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)
    final = agent.run("Read the isolated fixture.", history=[], on_sentence=lambda _x: None)
    captured = capsys.readouterr()
    assert final == "Read completed."
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in str(agent.exported_history())


def test_invalid_high_risk_call_is_rejected_before_confirmation(monkeypatch):
    dispatches = []
    confirmations = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "run_command",
        lambda command, timeout=None: dispatches.append(command) or "unexpected",
    )
    monkeypatch.setattr(
        "osia.action_policy.guard_tool_call",
        lambda *_a, **_k: confirmations.append(1) or None,
    )
    agent = OSIAAgent(lang="en")
    rounds = []
    _install_stream(agent, [
        '<tool_call>{"tool":"run_command","command":"echo private","extra":"bad"}</tool_call>',
        "The invalid call was rejected.",
    ], rounds)
    final = agent.run("Run a fixture.", history=[], on_sentence=lambda _x: None)
    assert "single repair attempt" in final
    assert dispatches == []
    assert confirmations == []


def test_late_completion_after_timeout_is_one_unknown_invocation(monkeypatch):
    calls = []
    completed = threading.Event()

    def late_write(path, content):
        calls.append((path, content))
        time.sleep(0.08)
        completed.set()
        return "late success"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", late_write)
    original_timeout = agent_module._tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_tool_timeout_seconds",
        lambda name, kwargs: 0.01 if name == "write_file" else original_timeout(name, kwargs),
    )
    record = agent_module._execute_tool_with_policy(
        "write_file", {"path": "opaque", "content": "private"}, lang="en",
    )
    assert record.outcome == ExecutionOutcome.OUTCOME_UNKNOWN
    assert record.attempts == 1
    assert len(calls) == 1
    assert completed.wait(1.0)
    assert len(calls) == 1


def _install_stream(agent, items, counter):
    remaining = iter(items)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        counter.append(1)
        item = next(remaining)
        if isinstance(item, BaseException):
            raise item
        return item

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_provider_fallback_remains_available_before_any_execution(monkeypatch):
    agent = OSIAAgent(lang="en")
    agent.provider = "provider_one"
    agent.model = "model_one"
    monkeypatch.setattr(
        "osia.config.get_setting",
        lambda key, default=None: True if key == "ai_hierarchy_enabled" else default,
    )
    monkeypatch.setattr(
        agent,
        "_resolve_route",
        lambda _text: (
            "medium",
            [("provider_one", "model_one"), ("provider_two", "model_two")],
        ),
    )
    switches = []

    def fake_set_model(model, provider=None):
        agent.provider = provider or agent.provider
        agent.model = model
        switches.append((agent.provider, model))

    monkeypatch.setattr(agent, "set_model", fake_set_model)
    rounds = []
    _install_stream(agent, [TimeoutError("synthetic transport timeout"), "Safe final."], rounds)
    final = agent.run("Read-only request.", history=[], on_sentence=lambda _x: None)
    assert final == "Safe final."
    assert len(rounds) == 2
    assert switches == [
        ("provider_one", "model_one"),
        ("provider_two", "model_two"),
    ]


@pytest.mark.parametrize("provider", ["deepseek", "groq", "chatgpt_account", "openai"])
def test_provider_fallback_is_blocked_after_side_effect_dispatch(
    monkeypatch, provider,
):
    calls = []
    secret = "WOR178_BARRIER_SECRET"
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda path, content: calls.append((path, content)) or "write verified",
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="en")
    agent.provider = provider
    agent.model = "wor178-fixture"
    rounds = []
    _install_stream(agent, [
        '<tool_call>{"tool":"write_file","path":"opaque","content":"WOR178_BARRIER_SECRET"}</tool_call>',
        TimeoutError("provider failed after dispatch"),
    ], rounds)
    visible = []
    final = agent.run("Write one isolated fixture.", history=[], on_sentence=visible.append)
    assert len(calls) == 1
    assert len(rounds) == 2
    assert "did not replay" in final.lower()
    assert secret not in final
    assert visible == [final]
    assert agent.last_run_diagnostic["reason_code"] == "provider_timeout"


def test_unknown_tool_outcome_stops_before_a_second_provider_round(monkeypatch):
    calls = []
    completed = threading.Event()

    def late_write(path, content):
        calls.append(1)
        time.sleep(0.08)
        completed.set()
        return "late"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", late_write)
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    original_timeout = agent_module._tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_tool_timeout_seconds",
        lambda name, kwargs: 0.01 if name == "write_file" else original_timeout(name, kwargs),
    )
    agent = OSIAAgent(lang="fr")
    rounds = []
    _install_stream(agent, [
        '<tool_call>{"tool":"write_file","path":"opaque","content":"private"}</tool_call>',
    ], rounds)
    visible = []
    final = agent.run("Écris le fichier isolé.", history=[], on_sentence=visible.append)
    assert len(rounds) == 1
    assert len(calls) == 1
    assert completed.wait(1.0)
    assert len(calls) == 1
    assert "incertain" in final.lower()
    assert visible == [final]
    assert agent.last_run_diagnostic["reason_code"] == "tool_timeout"


def test_ambiguous_effect_error_keeps_non_timeout_diagnostic(monkeypatch):
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda path, content: "ERROR: provider replied after possible effect",
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="en")
    rounds = []
    _install_stream(agent, [
        '<tool_call>{"tool":"write_file","path":"opaque","content":"private"}</tool_call>',
    ], rounds)
    final = agent.run("Write the isolated fixture.", history=[], on_sentence=lambda _x: None)
    assert len(rounds) == 1
    assert "uncertain" in final.lower()
    assert agent.last_run_diagnostic["reason_code"] == "task_execution_failure"
