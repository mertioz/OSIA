"""ChatGPT Account must expose and dispatch OSIA's canonical run_command."""

from osia.capability_federation import federated_capabilities
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    _CANONICAL_NATIVE_TOOL_NAMES,
    _TurnAttempt,
    _canonical_native_tools,
    _exact_canonical_native_call,
)
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.tool_contracts import get_tool_spec
from tests.test_wor305_phase3a_no_replay import dispatch


def test_available_run_command_is_in_provider_dynamic_tools_with_canonical_schema():
    capability = next(
        item for item in federated_capabilities("chatgpt_account")
        if item.identity == "run_command" and item.source == "osia"
    )
    assert capability.available

    tool = next(
        item for item in _canonical_native_tools()
        if item.get("name") == "run_command"
    )
    spec = get_tool_spec("run_command")
    assert tool == {
        "type": "function",
        "name": "run_command",
        "description": spec.capability,
        "inputSchema": spec.input_schema,
    }
    assert "bash" not in _CANONICAL_NATIVE_TOOL_NAMES


def test_execution_strategy_does_not_filter_run_command_from_dynamic_tools():
    baseline = _canonical_native_tools()
    unrelated_context = {
        "mode": "planning-only",
        "relevance": "unrelated",
        "strategy_revision": 0,
    }
    assert _canonical_native_tools(unrelated_context) == baseline
    assert any(item.get("name") == "run_command" for item in baseline)


def test_thread_start_serializes_the_run_command_dynamic_tool_schema(tmp_path):
    client = AppServerClient(
        base_dir=tmp_path / "codex",
        compact_logs_on_startup=False,
    )
    requests = []

    def capture(method, params, **_kwargs):
        requests.append((method, params))
        return {"thread": {"id": "thread-run-command"}}

    client._request = capture
    tools = _canonical_native_tools()
    thread_id = client.start_or_resume_conversation(
        thread_id=None,
        model="gpt-5.6-sol",
        base_instructions="base",
        developer_instructions="developer",
        dynamic_tools=tools,
    )

    assert thread_id == "thread-run-command"
    assert len(requests) == 1
    method, params = requests[0]
    assert method == "thread/start"
    assert params["dynamicTools"] == tools
    serialized = next(
        item for item in params["dynamicTools"]
        if item.get("name") == "run_command"
    )
    spec = get_tool_spec("run_command")
    assert serialized["type"] == "function"
    assert serialized["description"] == spec.capability
    assert serialized["inputSchema"] == spec.input_schema
    assert serialized["inputSchema"]["required"] == ["command"]
    assert serialized["inputSchema"]["additionalProperties"] is False


def test_mixed_run_command_and_visual_frontier_uses_code_mode_client(monkeypatch):
    class FakeClient:
        generation = 1

        def __init__(self, name):
            self.name = name
            self.started = []

        def start_or_resume_conversation(self, **kwargs):
            self.started.append(kwargs)
            return f"thread-{self.name}"

    ordinary = FakeClient("ordinary")
    visual = FakeClient("visual")
    provider = ChatGPTAccountProvider(
        ordinary, direct_visual_client=visual,
    )
    dynamic_tools = [
        {
            "type": "function",
            "name": "run_command",
            "description": "Execute one local shell command",
            "inputSchema": get_tool_spec("run_command").input_schema,
        },
        {
            "type": "function",
            "name": "computer_screenshot",
            "description": "Capture one screen",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]
    monkeypatch.setattr(
        "osia.chatgpt_account.provider._canonical_native_tools",
        lambda _strategy: dynamic_tools,
    )

    state, _delta = provider._prepare(
        "conversation-command-visual",
        "fixture-model",
        [{"role": "user", "content": "Print a harmless marker."}],
    )

    assert state.runtime_client is ordinary
    assert ordinary.started[0]["dynamic_tools"] == tuple(dynamic_tools)
    assert visual.started == []


def test_codex_01561_dynamic_call_shape_hands_run_command_to_canonical_pipeline(tmp_path):
    attempt = _TurnAttempt(thread_id="provider-thread", turn_id="provider-turn")
    params = {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "namespace": None,
        "callId": "run-command-call-1",
        "tool": "run_command",
        "arguments": {"command": "cmd /d /c echo SAFE"},
    }
    expected = {
        "tool": "run_command",
        "arguments": params["arguments"],
        "call_id": params["callId"],
    }
    parsed, failure = AppServerClient._dynamic_call_from_params(params)
    assert failure == ""
    assert parsed == expected
    assert _exact_canonical_native_call(params) == expected

    client = AppServerClient(
        base_dir=tmp_path / "codex",
        compact_logs_on_startup=False,
    )
    client._attempts[attempt.thread_id] = attempt
    sent = []
    interrupts = []
    client._send = sent.append
    client._send_request_no_wait = lambda method, values: interrupts.append(
        (method, values)
    )

    client._handle_server_request({
        "id": 31,
        "method": "item/tool/call",
        "params": params,
    })

    assert sent[0]["result"]["success"] is False
    assert "canonical tool pipeline" in sent[0]["result"]["contentItems"][0]["text"]
    assert attempt.native_tool_call == expected
    assert attempt.unsafe_category == "canonical_tool:run_command"
    assert interrupts == [("turn/interrupt", {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
    })]


def test_unknown_dynamic_tool_is_rejected_as_unavailable_without_dispatch(tmp_path):
    attempt = _TurnAttempt(thread_id="provider-thread", turn_id="provider-turn")
    client = AppServerClient(
        base_dir=tmp_path / "codex",
        compact_logs_on_startup=False,
    )
    client._attempts[attempt.thread_id] = attempt
    sent = []
    client._send = sent.append
    client._send_request_no_wait = lambda *_args: None
    params = {
        "threadId": attempt.thread_id,
        "turnId": attempt.turn_id,
        "callId": "unrelated-call-1",
        "tool": "unrelated_dynamic_tool",
        "arguments": {"value": "x"},
    }

    client._handle_server_request({
        "id": 32,
        "method": "item/tool/call",
        "params": params,
    })
    _kind, payload = attempt.events.get_nowait()
    client._resolve_dynamic_tool_call(attempt, payload)

    result = sent[-1]["result"]
    assert result["success"] is False
    assert attempt.native_tool_call is None
    assert not attempt.native_action
    assert attempt.capability_events[-1].reason_code == "CAPABILITY_UNAVAILABLE"
    assert attempt.capability_events[-1].outcome == "failed_before_effect"


def test_real_executor_receipt_returns_to_the_same_selected_model(
    dispatch, monkeypatch,
):
    from osia import agent as runtime
    from wor305_test_support import call, scripted

    marker = "WOR305_RUN_COMMAND_TEST_OK"
    command = f"echo {marker}"
    agent, calls = scripted(monkeypatch, [
        call("run_command", command=command),
        f"I observed {marker} in the command output.",
    ])
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-sol"

    executed = []
    original_execute = runtime._execute_tool_with_policy

    def capture_execution(*args, **kwargs):
        record = original_execute(*args, **kwargs)
        from osia.tool_attempts import get_tool_attempt_store

        context = kwargs.get("attempt_context")
        store = (
            context.get("store") or get_tool_attempt_store()
            if isinstance(context, dict) else get_tool_attempt_store()
        )
        row = (
            store.get_attempt(record.tool_attempt_id)
            if store is not None and record.tool_attempt_id else None
        )
        executed.append((record, row))
        return record

    monkeypatch.setattr(runtime, "_execute_tool_with_policy", capture_execution)

    final = agent.run_terminal_turn(
        f"Use run_command exactly once to print {marker}.",
        turn_id="phase4b8f",
    )

    assert final == f"I observed {marker} in the command output."
    assert len(calls) == 2
    assert all(call_args["model"] == "gpt-5.6-sol" for call_args in calls)
    assert marker in str(calls[1]["messages"])
    assert len(executed) == 1
    record, row = executed[0]
    assert record.tool_attempt_id
    assert record.dispatched and not record.is_error
    assert record.process_facts["exit_code"] == 0
    assert marker in record.process_facts["stdout"]
    assert row is not None
    assert row["action_truth"]["dispatch_started_at"]
    assert row["process_facts"]["exit_code"] == 0
    assert marker in row["process_facts"]["stdout"]
