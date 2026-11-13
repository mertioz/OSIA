"""Codex owns its model window, history compaction and exact native thread."""
import copy
import json
from types import SimpleNamespace

import pytest

from osia import conversation_context as context, conversations
from osia.chatgpt_account.app_server_client import AppServerClient, TurnResult
from osia.chatgpt_account.provider import ChatGPTAccountProvider


def _catalog(client, entries, *, version="0.167.0"):
    client.home_dir.mkdir(parents=True, exist_ok=True)
    (client.home_dir / "models_cache.json").write_text(json.dumps({
        "client_version": version, "models": entries,
    }), encoding="utf-8")


def _model(name="gpt-6.1-sol", *, default=500_000, maximum=872_000, percent=95):
    return {
        "slug": name, "context_window": default, "max_context_window": maximum,
        "effective_context_window_percent": percent,
    }


@pytest.mark.parametrize("name,maximum,usable", [
    ("gpt-6.1-sol", 872_000, 828_400),
    ("gpt-6-luna", 872_000, 828_400),
    ("future-model", 1_050_000, 997_500),
])
def test_native_start_and_resume_use_only_the_selected_models_catalog_window(tmp_path, monkeypatch, name, maximum, usable):
    client = AppServerClient(base_dir=tmp_path)
    _catalog(client, [_model(name, maximum=maximum), _model("other-model", maximum=2_000_000)])
    calls = []

    def request(method, params):
        calls.append((method, params))
        return {"thread": {"id": "exact-thread"}}

    monkeypatch.setattr(client, "_request", request)
    assert client.start_or_resume_conversation(
        thread_id=None, model=name, base_instructions="policy",
        developer_instructions="tools policy", persistent=True,
    ) == "exact-thread"
    client.start_or_resume_conversation(
        thread_id="exact-thread", model=name, base_instructions="",
        developer_instructions="", persistent=True,
    )
    assert [method for method, _ in calls] == ["thread/start", "thread/resume"]
    for _, params in calls:
        assert params["model"] == name
        assert params["config"]["model_context_window"] == maximum
        assert params["config"]["model_auto_compact_token_limit"] == usable * 95 // 100
        assert params["config"]["compact_prompt"]
    assert client.model_context_capacity(name)["maximum"] == usable
    assert client._process is None


def test_catalog_refresh_unknown_model_corruption_and_runtime_version_do_not_guess_upward(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    assert client.model_context_capacity("gpt-6.1-sol") is None
    _catalog(client, [_model()])
    assert client.model_context_capacity("gpt-6.1-sol")["maximum"] == 828_400
    assert client.model_context_capacity("unknown") is None
    _catalog(client, [_model(maximum=1_050_000)])
    assert client.model_context_capacity("gpt-6.1-sol")["maximum"] == 997_500
    client._resolved_runtime = (tmp_path / "codex", "codex-cli 0.168.0")
    assert client.model_context_capacity("gpt-6.1-sol") is None
    client._resolved_runtime = None
    (client.home_dir / "models_cache.json").write_text("{broken", encoding="utf-8")
    assert client.model_context_capacity("gpt-6.1-sol") is None
    assert client._thread_context_config("unknown", persistent=False) == {}
    assert client._process is None


@pytest.mark.parametrize("entry", [
    _model(default=True), _model(percent=True), _model(percent=0),
    _model(percent=101), {"slug": "gpt-6.1-sol", "max_context_window": 1_050_000},
])
def test_invalid_catalog_capabilities_leave_native_defaults_intact(tmp_path, entry):
    client = AppServerClient(base_dir=tmp_path)
    _catalog(client, [entry])
    assert client.model_context_capacity("gpt-6.1-sol") is None
    assert "model_context_window" not in client._thread_context_config("gpt-6.1-sol", persistent=True)["config"]


def test_context_estimate_uses_isolated_native_capabilities_without_starting_runtime(tmp_path, monkeypatch):
    from osia.chatgpt_account import provider
    client = AppServerClient(base_dir=tmp_path)
    _catalog(client, [_model()])
    monkeypatch.setattr(provider, "get_account_provider", lambda: SimpleNamespace(client=client))
    assert context.resolve_context_limit("chatgpt_account", "gpt-6.1-sol") == 828_400
    assert context.resolve_context_limit("openai", "gpt-6.1-sol") == 1_050_000
    assert client._process is None


@pytest.mark.parametrize("force", [False, True])
def test_native_history_never_receives_osias_local_summary_even_over_the_estimate(force):
    messages = [{"role": "system", "content": "policy"}]
    messages += [{
        "role": "user" if n % 2 == 0 else "assistant",
        "content": f"MARKER_{n} " + "historical evidence " * 700,
    } for n in range(20)]
    messages[-2]["content"] = "Next question"
    messages[-1]["content"] = "Latest answer"
    original = copy.deepcopy(messages)
    result = context.build_context(
        messages, provider="chatgpt_account", model="fixture-model",
        context_limit=8192, force_compaction=force,
    )
    assert result.messages == original == messages
    assert result.receipt["compaction_strategy"] == "provider_native"
    assert result.receipt["estimated_tokens_before"] > result.receipt["input_budget"]
    assert result.receipt["summarized_message_ids"] == []
    assert result.receipt["compacted_message_ids"] == []
    # The same input still uses OSIA's existing compactor on other providers.
    local = context.build_context(messages, provider="ollama", model="fixture", context_limit=8192)
    assert local.receipt["compaction_strategy"].startswith("deterministic_")


def test_manual_compaction_calls_codex_without_first_compacting_or_estimating_locally(tmp_path, monkeypatch):
    from osia.chatgpt_account import provider
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    conv_id = conversations.create_conversation()["id"]
    conversations.add_message(conv_id, "user", "Large current request " * 30_000)
    original = conversations.get_conversation(conv_id)["messages"]
    calls = []
    usage = {"used": 12_000, "maximum": 828_400, "estimated": False}
    account = SimpleNamespace(
        compact_conversation=lambda cid, model: calls.append((cid, model)) or True,
        conversation_context_usage=lambda cid, **kw: usage,
    )
    monkeypatch.setattr(provider, "get_account_provider", lambda: account)
    monkeypatch.setattr(context, "build_context", lambda *a, **kw: pytest.fail("local compaction ran"))
    result = context.compact_conversation_context(conv_id, "chatgpt_account", "gpt-6.1-sol")
    assert calls == [(conv_id, "gpt-6.1-sol")]
    assert result == {"changed": True, "usage": usage}
    stored = conversations.get_conversation(conv_id)
    assert stored["messages"][:-1] == original
    assert stored["messages"][-1]["_context_compaction_state"] == "completed"
    assert not conversations.context_compaction_active(stored)


def test_missing_native_thread_never_falls_back_to_a_local_success(tmp_path, monkeypatch):
    from osia.chatgpt_account import provider
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    conv_id = conversations.create_conversation()["id"]
    monkeypatch.setattr(provider, "get_account_provider", lambda: SimpleNamespace(compact_conversation=lambda *a: False))
    with pytest.raises(context.ContextBuildError, match="native_compaction_unavailable"):
        context.compact_conversation_context(conv_id, "chatgpt_account", "gpt-6-luna")
    assert conversations.get_conversation(conv_id)["messages"][-1]["_context_compaction_state"] == "failed"


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

    def model_context_capacity(self, model):
        raw = 872_000 if model == "large" else 272_000
        return {"context_window": raw, "maximum": raw * 95 // 100, "auto_compact_token_limit": raw * 90 // 100}

    def start_or_resume_conversation(self, **kw):
        self.starts.append(kw)
        return kw["thread_id"] or "same-native-thread"

    def run_turn(self, **kw):
        self.turns.append(kw)
        kw["on_attempt_registered"]()
        return TurnResult("answer", kw["thread_id"], "turn", "completed")

    def compact_thread(self, thread_id):
        self.compacted.append(thread_id)


def test_switching_windows_and_compacting_keeps_the_same_native_thread_and_new_model(tmp_path, monkeypatch):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    conv_id = conversations.create_conversation()["id"]
    runtime = _Runtime()
    provider = ChatGPTAccountProvider(runtime)
    first = [{"role": "user", "content": "First"}]
    result = provider.complete(conversation_id=conv_id, model="large", messages=first, osia_turn_id="first")
    provider.complete(conversation_id=conv_id, model="small", osia_turn_id="second", messages=[
        *first, {"role": "assistant", "content": result.text}, {"role": "user", "content": "Next"},
    ])
    assert [call["thread_id"] for call in runtime.starts] == [None, "same-native-thread"]
    assert runtime.turns[-1]["text"] == "Next"
    assert provider.compact_conversation(conv_id, "large")
    assert runtime.starts[-1]["thread_id"] == "same-native-thread"
    assert runtime.starts[-1]["model"] == "large"
    assert runtime.compacted == ["same-native-thread"]
    assert len(runtime.turns) == 2
