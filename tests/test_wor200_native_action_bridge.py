"""Focused WOR-200 native-action bridge regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import osia.agent as agent_module
from osia.agent import OSIAAgent
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    AppServerError,
    _TurnAttempt,
    _canonical_native_tools,
)
from osia.chatgpt_account.provider import (
    AccountProviderError,
    ChatGPTAccountProvider,
)


class _BridgeClient:
    generation = 1

    def __init__(self, error: AppServerError):
        self.error = error
        self.turns = []

    def start_or_resume_conversation(self, **_kwargs):
        return "thread-wor200"

    def run_turn(self, **kwargs):
        self.turns.append(kwargs)
        raise self.error


class _ContinuityBridgeClient(_BridgeClient):
    def __init__(self, error: AppServerError):
        super().__init__(error)
        self.starts = 0

    def start_or_resume_conversation(self, **_kwargs):
        self.starts += 1
        return "thread-wor200"

    def run_turn(self, **kwargs):
        self.turns.append(kwargs)
        if len(self.turns) == 1:
            raise self.error
        return SimpleNamespace(
            text="same-turn final",
            thread_id="thread-wor200",
            turn_id="turn-final",
            status="completed",
            output_started=True,
        )


class _SequenceCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        message = SimpleNamespace(content=outcome, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _AccountChatClient:
    def __init__(self, outcomes):
        self.completions = _SequenceCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)
        self.conversation_id = None

    def set_conversation_id(self, value):
        self.conversation_id = value


def _agent_with_account_outcomes(outcomes):
    agent = OSIAAgent(model="wor200-test", lang="en")
    client = _AccountChatClient(outcomes)
    agent.client = client
    agent.provider = "chatgpt_account"
    agent.model = "account-model"
    agent._api_key = ""
    agent._api_key_chain = []
    agent._primary_base_url = ""
    agent._fallback_base_url = None
    agent._active_base_url = ""
    return agent, client


def _native_error(tool, arguments, *, call_id="call-wor200"):
    return AppServerError(
        "ERROR: canonical OSIA tool handoff required.",
        native_action=True,
        native_tool_call={
            "tool": tool,
            "arguments": dict(arguments),
            "call_id": call_id,
        },
        allow_fallback=False,
        category=f"canonical_tool:{tool}",
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    (
        ("write_file", {"path": r"C:\synthetic\fixture.txt", "content": "safe"}),
        ("navigate_browser", {"url": "http://127.0.0.1:8765/"}),
        ("read_document", {"path": r"C:\synthetic\fixture.pdf"}),
    ),
)
def test_exact_file_browser_and_document_intents_map_to_canonical_tools(
    tool, arguments,
):
    client = _BridgeClient(_native_error(tool, arguments))
    provider = ChatGPTAccountProvider(client=client)
    deltas = []

    result = provider.complete(
        conversation_id=f"conv-{tool}",
        model="account-model",
        messages=[{"role": "user", "content": "perform the synthetic action"}],
        on_delta=deltas.append,
    )

    assert result.text == deltas[0]
    assert result.text.startswith("<tool_call>")
    payload = json.loads(result.text.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert payload == {"tool": tool, **arguments}
    assert len(client.turns) == 1


def test_advertised_native_surface_uses_authoritative_exact_schemas_only():
    tools = _canonical_native_tools()
    assert [tool["name"] for tool in tools] == [
        "read_file",
        "write_file",
        "navigate",
        "open_browser",
        "navigate_browser",
        "read_document",
        "query_document",
    ]
    assert all(tool["type"] == "function" for tool in tools)
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_mapped_result_is_reinjected_on_same_account_thread_and_osia_turn():
    arguments = {"path": r"C:\synthetic\fixture.txt"}
    client = _ContinuityBridgeClient(_native_error("read_file", arguments))
    provider = ChatGPTAccountProvider(client=client)
    first_messages = [{"role": "user", "content": "read the fixture"}]

    mapped = provider.complete(
        conversation_id="conv-continuity",
        model="account-model",
        messages=first_messages,
    )
    final = provider.complete(
        conversation_id="conv-continuity",
        model="account-model",
        messages=[
            *first_messages,
            {"role": "assistant", "content": mapped.text},
            {"role": "user", "content": "<tool_result>safe result</tool_result>"},
        ],
    )

    assert client.starts == 1
    assert len(client.turns) == 2
    assert client.turns[1]["text"] == "<tool_result>safe result</tool_result>"
    assert final.text == "same-turn final"


def test_duplicate_dynamic_events_handoff_once_and_never_reach_pc_executor():
    client = AppServerClient()
    attempt = _TurnAttempt(thread_id="thread-wor200", turn_id="turn-wor200")
    client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    native_executor = []
    client._send = sent.append
    client._send_request_no_wait = (
        lambda method, params: interrupts.append((method, params))
    )
    params = {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "callId": "call-duplicate",
        "tool": "write_file",
        "arguments": {"path": r"C:\synthetic\one.txt", "content": "one"},
    }

    with patch.dict(
        agent_module.TOOL_MAP,
        {"write_file": lambda **_kwargs: native_executor.append("executed")},
    ):
        client._handle_server_request({"id": 41, "method": "item/tool/call", "params": params})
        client._handle_server_request({"id": 42, "method": "item/tool/call", "params": params})

    assert attempt.events.qsize() == 1
    assert attempt.native_tool_call == {
        "tool": "write_file",
        "arguments": params["arguments"],
        "call_id": "call-duplicate",
    }
    assert len(interrupts) == 1
    assert native_executor == []
    assert [message["result"]["success"] for message in sent] == [False, False]


def test_dynamic_lifecycle_waits_for_exact_request_without_early_refusal():
    client = AppServerClient()
    attempt = _TurnAttempt(thread_id="thread-lifecycle", turn_id="turn-lifecycle")
    client._attempts[attempt.thread_id] = attempt
    client._send = lambda _message: None
    client._send_request_no_wait = lambda _method, _params: None
    lifecycle = {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "item": {
            "id": "call-lifecycle",
            "type": "dynamicToolCall",
            "tool": "read_file",
            "arguments": {"path": r"C:\synthetic\fixture.txt"},
        },
    }

    client._dispatch_notification("item/started", lifecycle)
    assert not attempt.native_action
    assert attempt.events.empty()

    client._handle_server_request({
        "id": 43,
        "method": "item/tool/call",
        "params": {
            "threadId": attempt.thread_id,
            "turnId": attempt.turn_id,
            "callId": "call-lifecycle",
            "tool": "read_file",
            "arguments": {"path": r"C:\synthetic\fixture.txt"},
        },
    })
    assert attempt.native_tool_call["tool"] == "read_file"
    assert attempt.events.qsize() == 1


@pytest.mark.parametrize(
    ("tool", "arguments"),
    (
        (
            "write_file",
            {"path": r"C:\synthetic\permit.txt", "content": "one"},
        ),
        ("navigate_browser", {"url": "http://127.0.0.1:8765/"}),
        ("read_document", None),
    ),
)
def test_mapped_action_uses_permission_and_exact_execution_permit_once(
    tool, arguments, tmp_path,
):
    calls = []
    user_request = "perform the synthetic action"
    if tool == "read_document":
        source = tmp_path / "permit.pdf"
        source.write_bytes(b"%PDF-1.4\n%%EOF\n")
        arguments = {"path": str(source)}
        user_request = f'inspect the document "{source}"'
    tool_xml = f"<tool_call>{json.dumps({'tool': tool, **arguments})}</tool_call>"
    agent, client = _agent_with_account_outcomes([tool_xml, "mapped action complete"])

    with (
        patch.dict(
            agent_module.TOOL_MAP,
            {tool: lambda **kwargs: calls.append(kwargs) or "action verified"},
        ),
        patch("osia.action_policy.guard_tool_call", return_value=None) as permission,
        patch(
            "osia.action_policy.pending_execution_permit_matches",
            return_value=True,
        ) as permit_match,
        patch(
            "osia.action_policy.consume_pending_execution_permit",
            return_value=True,
        ) as permit_consume,
    ):
        result = agent.run(
            user_request,
            history=[],
            current_conversation_id="conv-permit",
        )

    assert result == "mapped action complete"
    assert len(calls) == 1
    dispatched_arguments = calls[0]
    assert dispatched_arguments == arguments
    permission.assert_called_once()
    permit_match.assert_called_once_with(tool, dispatched_arguments)
    permit_consume.assert_called_once_with(tool, dispatched_arguments)
    assert len(client.completions.calls) == 2


def test_unsupported_native_intent_reformulates_once_then_warns_once():
    refusal = (
        "OSIA could not translate this native action safely. No action was "
        "performed, and no exact OSIA tool is available."
    )
    unsupported = AccountProviderError(AppServerError(
        refusal,
        native_action=True,
        controlled_refusal=True,
        reformulatable_native_action=True,
        allow_fallback=False,
        category="native_item:commandExecution",
    ))
    agent, client = _agent_with_account_outcomes([unsupported, unsupported])
    finals = []

    result = agent.run(
        "perform an unsupported native action",
        history=[],
        on_sentence=finals.append,
        current_conversation_id="conv-unsupported",
    )

    assert result == refusal
    assert finals == [refusal]
    assert len(client.completions.calls) == 2
    correction_frames = [
        message for message in agent.messages
        if message.get("_internal")
        and "single allowed reformulation attempt" in str(message.get("content"))
    ]
    assert len(correction_frames) == 1
    assert sum(
        message.get("role") == "assistant" and message.get("content") == refusal
        for message in agent.messages
    ) == 1


def test_mapped_outcome_unknown_stops_before_provider_continuation():
    calls = []
    tool_xml = (
        '<tool_call>{"tool":"navigate_browser",'
        '"url":"http://127.0.0.1:8765/"}</tool_call>'
    )
    agent, client = _agent_with_account_outcomes([tool_xml, "must not run"])

    def unknown_browser(**kwargs):
        calls.append(kwargs)
        return {
            "text": "navigation outcome unknown",
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_unknown",
                "outcome": "outcome_unknown",
            },
        }

    with (
        patch.dict(agent_module.TOOL_MAP, {"navigate_browser": unknown_browser}),
        patch("osia.action_policy.guard_tool_call", return_value=None),
        patch("osia.action_policy.pending_execution_permit_matches", return_value=False),
    ):
        result = agent.run(
            "navigate once",
            history=[],
            current_conversation_id="conv-unknown",
        )

    assert calls == [{"url": "http://127.0.0.1:8765/"}]
    assert len(client.completions.calls) == 1
    assert "uncertain" in result.lower()


def test_successful_mapped_side_effect_blocks_provider_fallback():
    calls = []
    switches = []
    late_error = AccountProviderError(AppServerError(
        "ERROR: late transport failure",
        allow_fallback=True,
        category="process_crash",
    ))
    tool_xml = (
        '<tool_call>{"tool":"write_file","path":"C:\\\\synthetic\\\\once.txt",'
        '"content":"one"}</tool_call>'
    )
    agent, client = _agent_with_account_outcomes([tool_xml, late_error])

    def retain_account_model(model, provider=None):
        switches.append((provider, model))

    with (
        patch.dict(
            agent_module.TOOL_MAP,
            {"write_file": lambda **kwargs: calls.append(kwargs) or "write verified"},
        ),
        patch.object(
            agent,
            "_resolve_route",
            return_value=(
                "medium",
                [("chatgpt_account", "account-model"), ("openai", "gpt-test")],
            ),
        ),
        patch.object(agent, "set_model", side_effect=retain_account_model),
        patch(
            "osia.config.get_setting",
            side_effect=lambda key, default=None: (
                True if key == "ai_hierarchy_enabled" else default
            ),
        ),
        patch("osia.action_policy.guard_tool_call", return_value=None),
        patch("osia.action_policy.pending_execution_permit_matches", return_value=False),
    ):
        result = agent.run(
            "write the file once",
            history=[],
            current_conversation_id="conv-no-fallback",
        )

    assert calls == [{"path": r"C:\synthetic\once.txt", "content": "one"}]
    assert switches == [("chatgpt_account", "account-model")]
    assert len(client.completions.calls) == 2
    assert "did not replay" in result.lower()


def test_api_provider_does_not_gain_native_reformulation_behavior():
    unsupported = AccountProviderError(AppServerError(
        "native error",
        native_action=True,
        reformulatable_native_action=True,
        allow_fallback=False,
    ))
    agent, client = _agent_with_account_outcomes([unsupported])
    agent.provider = "openai"

    with pytest.raises(AccountProviderError):
        agent.run("ordinary API turn", history=[])

    assert len(client.completions.calls) == 1
