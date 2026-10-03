import asyncio
import json
import threading
import time
import types

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent
from osia.tool_attempts import (
    ToolAttemptCoordinator,
    ToolAttemptStatus,
    ToolAttemptStore,
    WAITING_TOOL_RESULT,
)
from osia.tool_contracts import ExecutionOutcome, get_tool_spec
from osia.turn_context import fixed_turn_context


@pytest.fixture
def isolated_attempt_runtime(tmp_path, monkeypatch):
    from osia import tool_attempts

    store = ToolAttemptStore(tmp_path / "attempts")
    coordinator = ToolAttemptCoordinator(store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    return store, coordinator


def _context(store, terminal_callback):
    return {
        "enabled": True,
        "store": store,
        "conversation_id": "conv_wait_fixture",
        "turn_id": "turn_wait_fixture",
        "request_id": "request_wait_fixture",
        "tool_call_id": "toolcall_wait_fixture",
        "provider_id": "fixture-provider",
        "model_id": "fixture-model",
        "turn_context": fixed_turn_context(
            "en", source="fixture", reference="turn_wait_fixture",
        ),
        "final_response_message_id": "assistant_wait_fixture",
        "terminal_callback": terminal_callback,
    }


def test_timeout_is_pending_and_late_completion_is_reconciled_once(
    isolated_attempt_runtime, monkeypatch,
):
    store, _coordinator = isolated_attempt_runtime
    calls = []
    terminal = []
    completed = threading.Event()
    release_worker = threading.Event()

    def delayed(path, content):
        calls.append((path, content))
        assert release_worker.wait(5.0)
        return "late synthetic success"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", delayed)
    original_timeout = agent_module._tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_tool_timeout_seconds",
        lambda name, kwargs: 0.01 if name == "write_file" else original_timeout(name, kwargs),
    )

    def callback(attempt_id, record):
        terminal.append((attempt_id, record))
        completed.set()

    context = _context(store, callback)
    record = agent_module._execute_tool_with_policy(
        "write_file",
        {"path": "synthetic.txt", "content": "synthetic"},
        lang="en",
        attempt_context=context,
    )
    assert record.outcome == ExecutionOutcome.PENDING
    assert record.worker_may_be_active is True
    assert record.dispatched is True
    assert record.tool_attempt_id
    assert store.get_attempt(record.tool_attempt_id)["status"] == "pending"
    release_worker.set()
    assert completed.wait(1.0)
    durable = store.get_attempt(record.tool_attempt_id)
    assert durable["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert len(calls) == 1
    assert len(terminal) == 1


def test_authoritative_browser_timeouts_are_not_truncated_to_30_seconds():
    for tool_name, expected in {
        "activate_browser_element": 67.0,
        "wait_for_browser_condition": 67.0,
        "upload_browser_file": 92.0,
        "inspect_browser_page": 22.0,
        "inspect_form_fields": 12.0,
        "fill_form_field": 24.0,
        "fill": 47.0,
    }.items():
        assert agent_module._configured_tool_timeout_seconds(
            get_tool_spec(tool_name), tool_name, {},
        ) == expected
    assert agent_module._configured_tool_timeout_seconds(
        get_tool_spec("get_system_info"), "get_system_info", {},
    ) == 30


def test_activation_deadline_outlives_80_second_wait_and_owner_budget():
    arguments = {
        "wait_spec": {
            "condition_type": "text_appearance",
            "expected": "ready",
            "timeout_seconds": 80.0,
        }
    }
    spec = get_tool_spec("activate_browser_element")
    outer = agent_module._configured_tool_timeout_seconds(
        spec, "activate_browser_element", arguments,
    )
    owner = outer - agent_module.TOOL_OWNER_TRANSPORT_MARGIN_SECONDS
    durable = agent_module._durable_tool_deadline_seconds(
        spec, "activate_browser_element", arguments, outer,
    )

    assert owner == 85.0
    assert outer == 87.0
    assert durable == 89.0
    assert durable > arguments["wait_spec"]["timeout_seconds"]
    assert durable > owner


def test_80_second_wait_contract_can_complete_once_without_replay(
    isolated_attempt_runtime, monkeypatch,
):
    store, _coordinator = isolated_attempt_runtime
    calls = []
    state_ref = {
        "schema_version": 1,
        "browser_session_id": "browser_AAAAAAAAAAAAAAAAAAAAAAAA",
        "context_id": "context_AAAAAAAAAAAAAAAAAAAAAAAA",
        "context_epoch": 1,
        "page_id": "page_AAAAAAAAAAAAAAAAAAAAAAAA",
        "page_generation": 1,
        "navigation_id": "nav_AAAAAAAAAAAAAAAAAAAAAAAA",
        "document_epoch": 1,
        "created_at": 1.0,
    }
    arguments = {
        "state_ref": state_ref,
        "element_ref": "el_AAAAAAAAAAAAAAAAAAAAAAAA",
        "wait_spec": {
            "condition_type": "text_appearance",
            "expected": "ready after eighty seconds",
            "timeout_seconds": 80.0,
        },
    }

    def verified_once(**kwargs):
        calls.append(kwargs["wait_spec"]["timeout_seconds"])
        return {
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
            }
        }

    monkeypatch.setitem(
        agent_module.TOOL_MAP, "activate_browser_element", verified_once,
    )
    monkeypatch.setattr(
        "osia.action_policy.consume_pending_execution_permit",
        lambda *_a, **_k: True,
    )
    context = _context(store, lambda *_args: True)
    record = agent_module._execute_tool_with_policy(
        "activate_browser_element",
        arguments,
        lang="en",
        enforce_execution_permit=True,
        attempt_context=context,
    )

    assert record.outcome == ExecutionOutcome.SUCCESS_VERIFIED
    assert calls == [80.0]
    durable = store.get_attempt(record.tool_attempt_id)
    assert durable["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert durable["deadline_at"] - durable["accepted_at"] >= 88.0


def test_unverified_postcondition_mutation_suspends_before_provider_continuation(
    isolated_attempt_runtime, monkeypatch,
):
    store, coordinator = isolated_attempt_runtime
    action_calls = []
    verification_calls = []
    provider_rounds = []
    finals = []
    finalized = threading.Event()

    def navigate_once(url, state_ref=None, wait_spec=None):
        action_calls.append((url, state_ref, wait_spec))
        return "controlled action accepted"

    monkeypatch.setitem(agent_module.TOOL_MAP, "navigate_browser", navigate_once)
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    from osia.tools import browser_controller as browser_tools

    monkeypatch.setattr(
        browser_tools,
        "get_browser_state",
        lambda: {"data": {"state": {"schema_version": 1}}},
    )

    def verify_only(state, wait_spec):
        verification_calls.append((state, wait_spec))
        return {
            "ok": True,
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
            },
        }

    monkeypatch.setattr(browser_tools, "wait_for_browser_condition", verify_only)
    outputs = iter([
        '<tool_call>{"tool":"navigate_browser","url":"http://127.0.0.1/fixture",'
        '"wait_spec":{"condition_type":"text_appearance","expected":"ready",'
        '"timeout_seconds":10}}'
        '</tool_call>',
        "One verified final.",
    ])
    agent = OSIAAgent(model="fixture-model", lang="en")
    agent.provider = "fixture-provider"

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        provider_rounds.append((self.provider, self.model, list(self.messages)))
        return next(outputs)

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)
    agent.configure_tool_attempts(
        conversation_id="conv_wait_fixture",
        turn_id="turn_wait_fixture",
        request_id="request_wait_fixture",
        final_response_message_id="assistant_wait_fixture",
        final_callback=lambda attempt_id, text, error: (
            finals.append((attempt_id, text, error)), finalized.set(), True
        )[-1],
        on_sentence=lambda _text: None,
    )
    turn_context = fixed_turn_context(
        "en", source="fixture", reference="turn_wait_fixture",
    )

    first = agent.run(
        "Run the controlled action once and wait for its postcondition.",
        history=[],
        on_sentence=lambda _text: None,
        current_conversation_id="conv_wait_fixture",
        turn_context=turn_context,
    )

    assert first == WAITING_TOOL_RESULT
    assert len(action_calls) == 1
    assert len(provider_rounds) == 1
    assert finals == []
    attempt_id = agent.last_pending_tool_attempt_id
    durable = store.get_attempt(attempt_id)
    assert durable["status"] == ToolAttemptStatus.WAITING_VERIFICATION.value
    assert durable["dispatch_state"] == "owner_settled_unverified"
    assert durable["reason_code"] == "postcondition_required_unverified"
    assert not any(
        message.get("role") == "assistant" and not message.get("_provisional")
        for message in agent.messages[1:]
    )

    assert agent.activate_pending_tool_attempt() is True
    assert coordinator.request_reconciliation("conv_wait_fixture") is True
    assert finalized.wait(2.0)
    assert len(action_calls) == 1
    assert len(verification_calls) == 1
    assert len(provider_rounds) == 2
    assert len(finals) == 1
    assert finals[0][1] == "One verified final."
    completed = store.get_attempt(attempt_id)
    assert completed["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert completed["reason_code"] == "postcondition_verified_read_only"
    assert completed["provider_id"] == "fixture-provider"
    assert completed["model_id"] == "fixture-model"
    assert completed["turn_id"] == "turn_wait_fixture"


def test_semantic_fill_owner_callback_outlives_outer_wait_without_value_leak(
    isolated_attempt_runtime, monkeypatch,
):
    store, _coordinator = isolated_attempt_runtime
    private_value = "PRIVATE_FILL_VALUE_MUST_NOT_PERSIST"
    calls = []
    terminal = []
    completed = threading.Event()

    def delayed_fill(field_ref, value):
        calls.append(field_ref)
        assert value == private_value
        time.sleep(0.08)
        return json.dumps({
            "ok": True,
            "verified": True,
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
                "reason_code": "semantic_fill_verified",
            },
        })

    monkeypatch.setitem(agent_module.TOOL_MAP, "fill_form_field", delayed_fill)
    monkeypatch.setitem(
        agent_module._BROWSER_OWNER_TIMEOUT_SECONDS, "fill_form_field", 0.01,
    )
    monkeypatch.setattr(agent_module, "TOOL_OWNER_TRANSPORT_MARGIN_SECONDS", 0.01)

    def callback(attempt_id, record):
        terminal.append((attempt_id, record))
        completed.set()

    context = _context(store, callback)
    record = agent_module._execute_tool_with_policy(
        "fill_form_field",
        {"field_ref": "fld_AAAAAAAAAAAAAAAAAAAAAAAA", "value": private_value},
        lang="en",
        attempt_context=context,
    )

    assert record.outcome == ExecutionOutcome.PENDING
    assert record.worker_may_be_active is True
    pending = store.get_attempt(record.tool_attempt_id)
    assert pending["status"] == ToolAttemptStatus.PENDING.value
    assert pending["deadline_at"] - time.time() > 9.0
    assert completed.wait(1.0)
    assert len(calls) == 1
    assert len(terminal) == 1
    assert store.get_attempt(record.tool_attempt_id)["status"] == (
        ToolAttemptStatus.SUCCEEDED.value
    )
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in store.attempts_dir.glob("*.json")
    )
    assert private_value not in persisted


def test_uncertain_semantic_fill_enters_reconciliation_and_cannot_replay(
    isolated_attempt_runtime, monkeypatch,
):
    store, coordinator = isolated_attempt_runtime
    calls = []
    private_value = "PRIVATE_UNCERTAIN_VALUE"

    def uncertain_fill(field_ref, value):
        calls.append(field_ref)
        assert value == private_value
        return (
            'ERROR: {"category":"unverified_result","ok":false,'
            '"receipt":{"dispatch_state":"effect_unknown",'
            '"outcome":"outcome_unknown","reason_code":"unverified_result",'
            '"schema_version":1}}'
        )

    monkeypatch.setitem(agent_module.TOOL_MAP, "fill_form_field", uncertain_fill)
    context = _context(store, coordinator.notify_terminal)
    arguments = {
        "field_ref": "fld_AAAAAAAAAAAAAAAAAAAAAAAA",
        "value": private_value,
    }

    first = agent_module._execute_tool_with_policy(
        "fill_form_field", arguments, lang="en", attempt_context=context,
    )
    second = agent_module._execute_tool_with_policy(
        "fill_form_field", arguments, lang="en", attempt_context=context,
    )

    assert first.outcome == ExecutionOutcome.PENDING
    assert store.get_attempt(first.tool_attempt_id)["status"] == (
        ToolAttemptStatus.WAITING_VERIFICATION.value
    )
    assert second.outcome == ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert calls == ["fld_AAAAAAAAAAAAAAAAAAAAAAAA"]
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in store.attempts_dir.glob("*.json")
    )
    assert private_value not in persisted


def test_normal_chat_waits_then_resumes_same_turn_once(
    isolated_attempt_runtime, monkeypatch,
):
    store, _coordinator = isolated_attempt_runtime
    calls = []
    provider_rounds = []
    statuses = []
    finals = []
    final_event = threading.Event()

    def delayed(path, content):
        calls.append((path, content))
        time.sleep(0.08)
        return "late synthetic success"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", delayed)
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
    outputs = iter([
        '<tool_call>{"tool":"write_file","path":"synthetic.txt","content":"synthetic"}</tool_call>',
        "Canonical resumed final.",
    ])
    agent = OSIAAgent(model="fixture-model", lang="en")
    agent.provider = "fixture-provider"

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        provider_rounds.append((
            self.provider,
            self.model,
            self.turn_context.turn_ref,
            list(self.messages),
        ))
        return next(outputs)

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)
    turn_context = fixed_turn_context(
        "en", source="fixture", reference="turn_wait_fixture",
    )

    def finalize(attempt_id, final_text, error):
        finals.append((attempt_id, final_text, error))
        final_event.set()
        return True

    agent.configure_tool_attempts(
        conversation_id="conv_wait_fixture",
        turn_id="turn_wait_fixture",
        request_id="request_wait_fixture",
        final_response_message_id="assistant_wait_fixture",
        status_callback=lambda status, attempt_id: statuses.append((status, attempt_id)),
        final_callback=finalize,
        on_sentence=lambda _text: None,
    )
    first = agent.run(
        "Write the synthetic fixture once.",
        history=[],
        on_sentence=lambda _text: None,
        current_conversation_id="conv_wait_fixture",
        turn_context=turn_context,
    )
    assert first == WAITING_TOOL_RESULT
    dispatch_deadline = time.time() + 0.5
    while not calls and time.time() < dispatch_deadline:
        time.sleep(0.005)
    assert calls == [("synthetic.txt", "synthetic")]
    assert not any(
        message.get("role") == "assistant"
        and not message.get("_provisional")
        for message in agent.messages[1:]
    )
    assert agent.activate_pending_tool_attempt() is True
    assert final_event.wait(2.0)
    assert len(calls) == 1
    assert len(finals) == 1
    assert finals[0][1] == "Canonical resumed final."
    assert finals[0][2] is None
    assert len(provider_rounds) == 2
    assert {(provider, model) for provider, model, _ref, _messages in provider_rounds} == {
        ("fixture-provider", "fixture-model")
    }
    assert {ref for _provider, _model, ref, _messages in provider_rounds} == {
        turn_context.turn_ref
    }
    continuation_messages = provider_rounds[1][3]
    assert any(
        "tool_attempt_terminal" in str(message.get("content"))
        and "must_not_replay" in str(message.get("content"))
        for message in continuation_messages
    )
    deadline = time.time() + 1.0
    attempt = store.get_attempt(finals[0][0])
    while attempt["continuation_state"] != "complete" and time.time() < deadline:
        time.sleep(0.01)
        attempt = store.get_attempt(finals[0][0])
    assert attempt["status"] == ToolAttemptStatus.SUCCEEDED.value
    assert attempt["continuation_state"] == "complete"
    assert attempt["final_response_message_id"] == "assistant_wait_fixture"
    assert [status for status, _attempt_id in statuses].count(
        "resuming_tool_turn"
    ) == 1


def test_cancellation_after_dispatch_tombstones_authority_and_keeps_late_evidence(
    isolated_attempt_runtime, monkeypatch,
):
    store, coordinator = isolated_attempt_runtime
    completed = threading.Event()
    dispatched = threading.Event()
    release = threading.Event()

    def delayed(path, content):
        dispatched.set()
        assert release.wait(1.0)
        return "late success"

    monkeypatch.setitem(agent_module.TOOL_MAP, "write_file", delayed)
    original_timeout = agent_module._tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_tool_timeout_seconds",
        lambda name, kwargs: 0.01 if name == "write_file" else original_timeout(name, kwargs),
    )
    context = _context(store, lambda _attempt, _record: completed.set())
    record = agent_module._execute_tool_with_policy(
        "write_file",
        {"path": "synthetic.txt", "content": "synthetic"},
        lang="en",
        attempt_context=context,
    )
    assert dispatched.wait(1.0)
    store.request_cancellation(record.tool_attempt_id)
    release.set()
    assert completed.wait(1.0)
    durable = store.get_attempt(record.tool_attempt_id)
    assert durable["status"] == ToolAttemptStatus.CANCELLED_PROVEN.value
    assert durable["authority_state"] == "cancelled"
    assert durable["reason_code"] == "late_result_after_cancellation"
    assert durable["action_truth"]["effect_truth"] == "verified"


def test_deadline_timer_wins_once_and_late_callback_is_ignored(
    isolated_attempt_runtime,
):
    store, coordinator = isolated_attempt_runtime
    context = _context(store, coordinator.notify_terminal)
    fingerprint = store.fingerprint_arguments({"synthetic": True})
    created = store.create_attempt(
        conversation_id=context["conversation_id"],
        turn_id=context["turn_id"],
        request_id=context["request_id"],
        tool_call_id=context["tool_call_id"],
        tool_name="activate_browser_element",
        provider_id=context["provider_id"],
        model_id=context["model_id"],
        turn_context={"language": "en", "source": "fixture"},
        argument_fingerprint=fingerprint,
        side_effect="external_mutation",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        deadline_at=time.time() - 1.0,
        final_response_message_id=context["final_response_message_id"],
    )
    accepted = store.mark_accepted(created["tool_attempt_id"])
    running = store.mark_running(accepted["tool_attempt_id"])
    store.mark_pending(
        running["tool_attempt_id"],
        next_check_at=time.time() - 1.0,
        deadline_at=time.time() - 0.5,
    )
    resumes = []
    finished = threading.Event()
    coordinator.register_suspended(
        running["tool_attempt_id"],
        resume=lambda attempt_id, payload: resumes.append(
            (attempt_id, payload["status"])
        ) or "deadline final",
        finalize=lambda *_args: finished.set() or True,
    )

    assert coordinator.activate(running["tool_attempt_id"])
    assert finished.wait(2.0)
    durable = store.get_attempt(running["tool_attempt_id"])
    assert durable["status"] == ToolAttemptStatus.UNKNOWN_AFTER_START.value
    assert len(resumes) == 1

    coordinator.notify_terminal(running["tool_attempt_id"], durable)
    time.sleep(0.05)
    assert len(resumes) == 1


def test_durable_barrier_blocks_equivalent_mutation_in_same_turn(
    isolated_attempt_runtime, monkeypatch,
):
    store, coordinator = isolated_attempt_runtime
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "write_file",
        lambda path, content: calls.append((path, content)) or "synthetic success",
    )
    context = _context(store, coordinator.notify_terminal)
    arguments = {"path": "synthetic.txt", "content": "synthetic"}

    first = agent_module._execute_tool_with_policy(
        "write_file", arguments, lang="en", attempt_context=context,
    )
    second = agent_module._execute_tool_with_policy(
        "write_file", arguments, lang="en", attempt_context=context,
    )

    assert first.outcome in {
        ExecutionOutcome.SUCCESS_VERIFIED,
        ExecutionOutcome.SUCCESS_UNVERIFIED,
    }
    assert second.outcome == ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert calls == [("synthetic.txt", "synthetic")]


@pytest.mark.parametrize("command", [
    "reprends",
    "Continue.",
    "RELANCE!",
    "reprends et vérifie",
    "continue et vérifie l’action en cours",
    "relance la vérification de l'action en cours",
    "resume",
    "relaunch",
    "resume and verify",
    "Resume and verify the current action.",
    "continue checking the current action",
])
def test_resume_reconciliation_grammar_accepts_bounded_commands(command):
    from osia.server import server

    assert server._is_resume_reconciliation_command(command) is True


@pytest.mark.parametrize("command", [
    "continue la recette",
    "relance le navigateur sur une autre page",
    "resume this article",
    "résume",
    "résumé",
    "please resume and verify the current action",
    "continue checking the current action and then browse elsewhere",
    "the current action should resume",
])
def test_resume_reconciliation_grammar_rejects_unrelated_commands(command):
    from osia.server import server

    assert server._is_resume_reconciliation_command(command) is False


def test_waiting_command_requires_same_conversation_attempt_and_never_redispatches(
    monkeypatch,
):
    from osia import tool_attempts
    from osia.server import server

    class Store:
        def __init__(self):
            self.suspended = True

        def find_compatible_suspended_turn(self, conversation_id):
            if self.suspended and conversation_id == "conv_resume_fixture":
                return {"tool_attempt_id": "attempt_resume_fixture"}
            return None

    class Coordinator:
        def __init__(self):
            self.store = Store()
            self.reconciliations = []
            self.mutation_dispatches = 0

        def request_reconciliation(self, conversation_id):
            self.reconciliations.append(conversation_id)
            return True

        def request_cancellation(self, _conversation_id):
            return False

    class Channel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(dict(frame))

    coordinator = Coordinator()
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", coordinator)
    meta = server._new_turn_meta(
        "conv_resume_fixture", "request_resume_fixture",
    )
    server._ACTIVE_TURN_META_BY_CONV["conv_resume_fixture"] = meta

    async def exercise():
        channel = Channel()
        handled = await server._handle_waiting_tool_command(
            channel,
            "conv_resume_fixture",
            "Resume and verify the current action.",
        )
        assert handled is True
        assert coordinator.reconciliations == ["conv_resume_fixture"]
        assert coordinator.mutation_dispatches == 0
        assert channel.frames[-1]["status"] == "verifying"

        unrelated = await server._handle_waiting_tool_command(
            channel,
            "conv_resume_fixture",
            "continue la recette",
        )
        assert unrelated is False
        assert coordinator.reconciliations == ["conv_resume_fixture"]
        assert not any(
            frame["type"] == "conversation_busy"
            for frame in channel.frames
        )

        status = await server._handle_waiting_tool_command(
            channel,
            "conv_resume_fixture",
            "check current status",
        )
        assert status is True
        assert coordinator.reconciliations == ["conv_resume_fixture"]

        coordinator.store.suspended = False
        missing = await server._handle_waiting_tool_command(
            channel,
            "conv_resume_fixture",
            "resume",
        )
        assert missing is False
        assert coordinator.reconciliations == ["conv_resume_fixture"]

    try:
        asyncio.run(exercise())
    finally:
        server._ACTIVE_TURN_META_BY_CONV.pop("conv_resume_fixture", None)


def test_reconnect_rejects_unproven_waiting_tool_metadata():
    from osia.server import server

    class Channel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(dict(frame))

    async def exercise():
        conv_id = "conv_reconnect_fixture"
        request_id = "request_reconnect_fixture"
        meta = server._new_turn_meta(conv_id, request_id)
        meta["_tool_attempt_status"] = "waiting_tool_result"
        holder = asyncio.get_running_loop().create_future()
        server._ACTIVE_TURNS_BY_CONV[conv_id] = holder
        server._ACTIVE_TURN_META_BY_CONV[conv_id] = meta
        server._ACTIVE_CONV_BY_REQUEST[request_id] = conv_id
        try:
            channel = Channel()
            assert not await server._send_recovered_turn_state(channel, conv_id)
            assert channel.frames == []
        finally:
            server._ACTIVE_TURNS_BY_CONV.pop(conv_id, None)
            server._ACTIVE_TURN_META_BY_CONV.pop(conv_id, None)
            server._ACTIVE_CONV_BY_REQUEST.pop(request_id, None)
            if not holder.done():
                holder.set_result(None)

    asyncio.run(exercise())


def test_server_persists_no_final_until_resumed_callback(tmp_path, monkeypatch):
    from osia.server import server

    monkeypatch.setattr(
        server.conversations, "CONVERSATIONS_DIR", tmp_path / "conversations",
    )
    conv = server.conversations.create_conversation()
    meta = server._new_turn_meta(conv["id"], "request_wait_server")
    meta["tts_enabled"] = False

    class Channel:
        def __init__(self):
            self.frames = []

        async def send_json(self, frame):
            self.frames.append(dict(frame))

    class PendingAgent:
        provider = "fixture-provider"
        model = "fixture-model"

        def __init__(self):
            self.messages = [{"role": "system", "content": "fixture"}]
            self.last_run_artifacts = []
            self.last_run_diagnostic = None
            self._in_use = False
            self._chat_autonomous_context = None
            self.callbacks = None

        def configure_tool_attempts(self, **kwargs):
            self.callbacks = kwargs

        def set_model(self, model, provider=None):
            self.model = model
            self.provider = provider or self.provider

        def run(self, text, **kwargs):
            self.messages.extend([
                {"role": "user", "content": text},
                {
                    "role": "assistant",
                    "content": '<tool_call>{"tool":"fixture"}</tool_call>',
                    "_provisional": True,
                },
            ])
            return WAITING_TOOL_RESULT

        def pending_tool_attempt_record(self):
            return {
                "tool_attempt_id": "attempt_server_fixture",
                "status": ToolAttemptStatus.PENDING.value,
            }

        def terminality_result(self, _turn_id):
            return {
                "terminal_state": "success_verified",
                "final_text": "One canonical resumed final.",
                "reason_code": "success_verified",
            }

        def activate_pending_tool_attempt(self):
            def complete():
                self.callbacks["status_callback"](
                    "resuming_tool_turn", "attempt_server_fixture",
                )
                self.callbacks["final_callback"](
                    "attempt_server_fixture", "One canonical resumed final.", None,
                )

            threading.Thread(target=complete, daemon=True).start()
            return True

        def exported_history(self):
            return [
                message for message in self.messages[1:]
                if not message.get("_provisional")
            ]

        def clear_tool_attempts(self):
            self.callbacks = None

    async def exercise():
        channel = Channel()
        agent = PendingAgent()
        _history, returned = await server._run_agent_and_respond(
            channel,
            "accepted synthetic request",
            [],
            conv["id"],
            get_agent=lambda _conv_id: agent,
            turn_meta=meta,
        )
        assert returned == conv["id"]
        assert len(
            server.conversations.get_conversation(conv["id"])["messages"]
        ) == 1
        deadline = time.time() + 2.0
        while time.time() < deadline:
            stored = server.conversations.get_conversation(conv["id"])["messages"]
            if len(stored) == 2:
                break
            await asyncio.sleep(0.01)
        stored = server.conversations.get_conversation(conv["id"])["messages"]
        assert [(item["role"], item["content"]) for item in stored] == [
            ("user", "accepted synthetic request"),
            ("assistant", "One canonical resumed final."),
        ]
        statuses = [
            frame.get("status") for frame in channel.frames
            if frame.get("type") == "turn_status"
        ]
        assert "waiting_tool_result" in statuses
        assert statuses.count("done") == 1
        responses = [
            frame for frame in channel.frames if frame.get("type") == "response"
        ]
        assert len(responses) == 1
        assert responses[0]["content"] == "One canonical resumed final."

    asyncio.run(exercise())
