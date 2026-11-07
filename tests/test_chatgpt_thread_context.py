import json
from types import SimpleNamespace

from osia import conversations
from osia.chatgpt_account.app_server_client import AppServerClient, AppServerError, TurnResult, _TurnAttempt
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.tool_contracts import ExecutionOutcome


class _Runtime:
    generation = 1
    account_generation = 0

    def __init__(self):
        self.starts = []
        self.turns = []
        self.compacted = []

    def start(self):
        pass

    def get_account_status(self):
        return {"connected": True, "account": {"email": "fixture@example.invalid"}}

    def start_or_resume_conversation(self, **kwargs):
        self.starts.append(kwargs)
        return kwargs["thread_id"] or f"fixture-thread-{len(self.starts)}"

    def run_turn(self, **kwargs):
        self.turns.append(kwargs)
        kwargs["on_attempt_registered"]()
        return TurnResult(
            f"answer {len(self.turns)}", kwargs["thread_id"], "native-turn", "completed",
        )

    def compact_thread(self, thread_id):
        self.compacted.append(thread_id)


def test_same_chatgpt_thread_survives_prompt_model_and_runtime_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    conv_id = conversations.create_conversation()["id"]
    runtime = _Runtime()
    provider = ChatGPTAccountProvider(runtime)

    first = [
        {"role": "system", "content": "First prompt"},
        {"role": "user", "content": "First question"},
    ]
    result = provider.complete(
        conversation_id=conv_id, model="first-model", messages=first, osia_turn_id="first-turn",
    )
    assert runtime.starts[0]["persistent"] is True
    second = [
        {"role": "system", "content": "Updated prompt"},
        first[1],
        {"role": "assistant", "content": result.text},
        {"role": "user", "content": "Next question"},
    ]
    next_result = provider.complete(
        conversation_id=conv_id, model="next-model", messages=second, osia_turn_id="second-turn",
    )
    assert next_result.thread_id == result.thread_id
    assert len(runtime.starts) == 1
    assert runtime.turns[-1]["text"] == "Next question"
    assert "Updated prompt" in runtime.turns[-1]["application_context"]

    reopened = _Runtime()
    provider = ChatGPTAccountProvider(reopened)
    result_after_restart = provider.complete(
        conversation_id=conv_id, model="next-model", osia_turn_id="third-turn",
        messages=[
            second[0],
            {"role": "assistant", "content": next_result.text},
            {"role": "user", "content": "After restart"},
        ],
    )
    assert reopened.starts[0]["thread_id"] == result.thread_id == result_after_restart.thread_id
    assert reopened.turns[-1]["text"] == "After restart"
    assert provider.compact_conversation(conv_id, "next-model")
    assert reopened.compacted == [result.thread_id]
    assert len(reopened.turns) == 1


def test_native_context_meter_uses_last_usage_of_exact_turn(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client._attempts["native"] = _TurnAttempt("native", turn_id="current")
    params = {
        "threadId": "native", "turnId": "old",
        "tokenUsage": {
            "modelContextWindow": 1000,
            "last": {"totalTokens": 420}, "total": {"totalTokens": 10_000},
        },
    }
    client._dispatch_notification("thread/tokenUsage/updated", params)
    assert client.thread_context_usage("native") is None
    client._dispatch_notification("thread/tokenUsage/updated", {**params, "turnId": "current"})
    assert client.thread_context_usage("native") == {
        "used": 420, "maximum": 1000, "estimated": False, "compacting": False,
    }


def test_only_a_proven_missing_rollout_rebuilds_the_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    conv_id = conversations.create_conversation()["id"]
    messages = [{"role": "user", "content": "Existing canonical conversation"}]
    original = ChatGPTAccountProvider(_Runtime()).complete(
        conversation_id=conv_id, model="fixture-model", messages=messages, osia_turn_id="first",
    )

    class _MissingRuntime(_Runtime):
        def start_or_resume_conversation(self, **kwargs):
            if kwargs["thread_id"]:
                self.starts.append(kwargs)
                raise AppServerError("missing", category="thread_missing")
            return super().start_or_resume_conversation(**kwargs)

    runtime = _MissingRuntime()
    updated = ChatGPTAccountProvider(runtime).complete(
        conversation_id=conv_id, model="fixture-model", messages=messages, osia_turn_id="retry",
    )
    assert [call["thread_id"] for call in runtime.starts] == [original.thread_id, None]
    assert updated.thread_id != original.thread_id
    assert runtime.turns[0]["text"] == "Existing canonical conversation"


def test_manual_native_compaction_requires_same_thread_and_turn(monkeypatch, tmp_path):
    client = AppServerClient(base_dir=tmp_path)

    def request(method, params):
        assert method == "thread/compact/start" and params == {"threadId": "saved"}
        client._dispatch_notification("turn/started", {
            "threadId": "saved", "turn": {"id": "compact-turn"},
        })
        item = {"type": "contextCompaction", "id": "context-item"}
        client._dispatch_notification("item/completed", {
            "threadId": "other", "turnId": "compact-turn", "item": item,
        })
        assert not client._compaction_requests["saved"].compacted
        client._dispatch_notification("item/completed", {
            "threadId": "saved", "turnId": "compact-turn", "item": item,
        })
        client._dispatch_notification("turn/completed", {
            "threadId": "saved", "turn": {"id": "compact-turn", "status": "completed"},
        })
        return {}

    monkeypatch.setattr(client, "_request", request)
    client.compact_thread("saved")
    assert not client._compaction_requests


def test_uncertain_browser_action_returns_typed_result_in_the_same_native_turn(monkeypatch, tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    attempt = _TurnAttempt(
        "native", conversation_id="conversation", osia_turn_id="chat-turn",
        turn_id="native-turn", model_id="fixture-model",
    )
    client._attempts["native"] = attempt
    sent = []
    calls = []
    monkeypatch.setattr(client, "_send", sent.append)

    def dispatch(tool, arguments, **kwargs):
        calls.append((tool, arguments, kwargs))
        return SimpleNamespace(
            tool_attempt_id="fixture-attempt", reason_code="wait_timed_out",
            outcome=ExecutionOutcome.OUTCOME_UNKNOWN, is_error=True,
            result={
                "ok": False,
                "text": "The browser action was not confirmed",
                "receipt": {
                    "schema_version": 1, "outcome": "outcome_unknown",
                    "dispatch_state": "effect_unknown",
                },
            },
        )

    monkeypatch.setattr("osia.agent._execute_tool_with_policy", dispatch)
    request = {
        "id": 7, "method": "item/tool/call",
        "params": {
            "threadId": "native", "turnId": "native-turn", "callId": "open",
            "tool": "open_new_browser_tab", "arguments": {"url": "https://example.invalid/"},
        },
    }
    for _ in range(2):
        client._handle_server_request(request)
        event, payload = attempt.events.get_nowait()
        assert event == "dynamic_tool_call"
        client._resolve_dynamic_tool_call(attempt, payload)
    assert len(calls) == 1
    assert not attempt.interrupt_sent and attempt.native_tool_call is None
    result = json.loads(sent[-1]["result"]["contentItems"][0]["text"])
    assert result["status"] == result["result"]["typed_outcome"] == "outcome_unknown"


def test_deleted_conversation_releases_only_its_saved_native_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    first = conversations.create_conversation()["id"]
    second = conversations.create_conversation()["id"]
    assert conversations.store_chatgpt_thread(first, {"thread_id": "first-thread"})
    assert conversations.store_chatgpt_thread(second, {"thread_id": "second-thread"})
    deleted = []
    monkeypatch.setattr(
        "osia.chatgpt_account.provider.delete_saved_chatgpt_threads",
        lambda bindings: deleted.extend(bindings),
    )
    assert conversations.delete_conversation(first)
    assert deleted == [(first, "first-thread")]
    assert conversations.get_conversation(second)["_chatgpt_account_thread"]["thread_id"] == "second-thread"
