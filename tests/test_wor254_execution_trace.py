import json
from types import SimpleNamespace

from osia.agent import OSIAAgent
from osia.execution_trace import (
    MAX_LIVE_FRAMES,
    MAX_PERSISTED_EVENTS,
    MAX_SERIALIZED_BYTES,
    ExecutionTraceLedger,
    validate_persisted_trace,
)
from osia.relay_connector import (
    MAX_RELAY_TRACE_INCREMENTAL,
    MAX_RELAY_TRACE_SNAPSHOT_BYTES,
    MAX_RELAY_TRACE_SNAPSHOT_EVENTS,
    RelayBridge,
)
from osia.server import server


def _ledger(callback=None):
    return ExecutionTraceLedger(
        conversation_id="conv-254",
        request_id="request-254",
        turn_id="turn-254",
        callback=callback,
    )


def test_public_trace_is_allowlisted_bounded_and_terminal_tombstoned():
    frames = []
    ledger = _ledger(frames.append)
    ledger.record(
        kind="goal",
        display_key="execution.goal.complete_task",
        dedupe_key="goal",
    )
    for index in range(40):
        ledger.record(
            kind="action",
            display_key="execution.action.capability",
            dedupe_key=f"action:{index}",
        )
    ledger.record(
        kind="terminal",
        display_key="execution.terminal.success",
        terminal=True,
        dedupe_key="terminal",
    )
    assert ledger.record(
        kind="action",
        display_key="execution.action.shell",
    ) is None

    snapshot = ledger.snapshot()
    encoded = json.dumps(snapshot, separators=(",", ":")).encode()
    assert len(frames) == MAX_LIVE_FRAMES
    assert frames[-1]["terminal"] is True
    assert len(snapshot["events"]) <= MAX_PERSISTED_EVENTS
    assert snapshot["events"][-1]["kind"] == "terminal"
    assert len(encoded) <= MAX_SERIALIZED_BYTES
    assert validate_persisted_trace(snapshot) == snapshot


def test_trace_rejects_free_text_args_unknown_keys_and_bad_persistence():
    ledger = _ledger()
    assert ledger.record(
        kind="action",
        display_key="execution.action.shell",
        display_args={"command": "secret --token=value"},
    ) is None
    assert ledger.record(
        kind="action",
        display_key="execution.action.not_allowlisted",
    ) is None

    ledger.record(
        kind="goal",
        display_key="execution.goal.answer",
        dedupe_key="goal",
    )
    malicious = ledger.snapshot()
    malicious["events"][0]["raw_result"] = "private payload"
    assert validate_persisted_trace(malicious) is None


def test_ids_compaction_and_safe_lifecycle_dedup_are_deterministic():
    first = _ledger()
    second = _ledger()
    for ledger in (first, second):
        ledger.record(
            kind="goal",
            display_key="execution.goal.complete_task",
            dedupe_key="goal",
        )
        for index in range(30):
            ledger.record(
                kind="action",
                display_key="execution.action.capability",
                dedupe_key=f"invocation:{index}",
            )
    first_events = first.snapshot()["events"]
    second_events = second.snapshot()["events"]
    assert [event["event_id"] for event in first_events] == [
        event["event_id"] for event in second_events
    ]
    assert [event["sequence"] for event in first_events] == [
        event["sequence"] for event in second_events
    ]

    agent = object.__new__(OSIAAgent)
    agent._execution_trace_ledger = None
    agent._execution_trace_callback = None
    agent.configure_execution_trace(
        conversation_id="conv-254",
        request_id="request-254",
        turn_id="turn-254",
    )
    for _ in range(2):
        agent._emit_execution_trace(
            "waiting_permission",
            "execution.permission.waiting",
            tool_attempt_id="attempt-254",
            permission_request_id="attempt-254",
            dedupe_key="permission:attempt-254",
        )
    agent._emit_execution_trace_continuation(
        SimpleNamespace(next_strategy="retry_with_constraints")
    )
    agent._emit_execution_trace_continuation(
        SimpleNamespace(next_strategy="reconcile_unknown_read_only")
    )
    events = agent.execution_trace_snapshot("turn-254")["events"]
    assert sum(event["kind"] == "waiting_permission" for event in events) == 1
    assert sum(event["kind"] == "continuation" for event in events) == 1
    assert sum(event["kind"] == "reconciliation" for event in events) == 1
    assert not any(
        event["display"]["key"] == "execution.terminal.success"
        for event in events
    )


def test_secret_path_query_and_exception_fuzz_never_enters_public_projection():
    forbidden = (
        "Bearer secret-token-254",
        r"C:\\Users\\private\\oauth.json",
        "query=private+prompt&token=abc",
        "RuntimeError: provider leaked raw result",
    )
    ledger = _ledger()
    for index, value in enumerate(forbidden):
        assert ledger.record(
            kind="action",
            display_key="execution.action.connector",
            display_args={f"unsafe_{index}": value},
        ) is None
    encoded = json.dumps(ledger.snapshot())
    assert all(value not in encoded for value in forbidden)


def test_relay_sends_six_incremental_plus_one_compact_terminal():
    frames = []
    ledger = _ledger(frames.append)
    for index, (kind, display) in enumerate((
        ("goal", "execution.goal.complete_task"),
        ("action", "execution.action.preparing"),
        ("action", "execution.action.browser"),
        ("action", "execution.action.document"),
        ("action", "execution.action.filesystem"),
        ("action", "execution.action.connector"),
        ("result", "execution.result.completed"),
    )):
        ledger.record(kind=kind, display_key=display, dedupe_key=f"slot:{index}")
    ledger.record(
        kind="terminal",
        display_key="execution.terminal.success",
        terminal=True,
        dedupe_key="terminal",
    )

    bridge = RelayBridge("wor-254-token", "wss://relay.test/ws")
    relayed = [
        value for frame in frames
        if (value := bridge._relay_execution_trace_frame(frame)) is not None
    ]
    assert len(relayed[:-1]) == MAX_RELAY_TRACE_INCREMENTAL
    terminal = relayed[-1]
    assert terminal["terminal"] is True
    assert len(terminal["snapshot"]) <= MAX_RELAY_TRACE_SNAPSHOT_EVENTS
    assert any(
        event.get("event_id") == terminal["event_id"]
        for event in terminal["snapshot"]
    )
    assert len(json.dumps(terminal, separators=(",", ":")).encode()) <= (
        MAX_RELAY_TRACE_SNAPSHOT_BYTES
    )
    assert bridge._relay_execution_trace_frame(frames[-1]) is None


def test_history_exposes_only_a_valid_assistant_trace():
    ledger = _ledger()
    ledger.record(
        kind="goal",
        display_key="execution.goal.answer",
        dedupe_key="goal",
    )
    trace = ledger.snapshot()
    history = server._displayable_history([
        {
            "role": "assistant",
            "content": "Safe final",
            "_conversation_id": "conv-254",
            "_request_id": "request-254",
            "_turn_id": "turn-254",
            "_message_id": "message-254",
            "_execution_trace_v1": trace,
        },
        {
            "role": "assistant",
            "content": "No unsafe metadata",
            "_execution_trace_v1": {"raw_prompt": "do not expose"},
        },
    ])
    assert history[0]["execution_trace"] == trace
    assert "execution_trace" not in history[1]
