"""Regression coverage for restored context counts and factual compaction notices."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import conversations
from osia import conversation_context as context
from osia.chatgpt_account.app_server_client import AppServerClient, TurnResult
from osia.chatgpt_account.provider import ChatGPTAccountProvider


@pytest.fixture
def chats(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "chats")
    with context._USAGE_LOCK:
        context._USAGE_BY_CONVERSATION.clear()
    return tmp_path


def _native_log(client, thread_id, *, used=100_000, model="fixture-model", header_size=0):
    path = client.home_dir / "sessions" / "2026" / f"rollout-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"id": thread_id, "base_instructions": "private fixture " * header_size}},
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": used},
            "total_token_usage": {"total_tokens": 900_000},
            "model_context_window": 258_400,
        }}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _legacy_binding(conv_id, thread_id):
    assert conversations.store_chatgpt_thread(conv_id, {
        "thread_id": thread_id, "account_scope": "fixture-account-scope",
    })


@pytest.mark.parametrize("provider,model", [("openai", "gpt-4.1"), ("deepseek", "deepseek-flash")])
def test_api_context_measurement_survives_cache_loss_without_rewriting_history(chats, provider, model):
    conv = conversations.create_conversation()
    conv = conversations.add_message(
        conv["id"], "user", "Retained history. " * 5000,
        metadata={"_fixture_document_ref": "doc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    original = copy.deepcopy(conv)
    built = context.build_context(
        [{"role": "system", "content": "Application instructions"}, *conv["messages"]],
        provider=provider, model=model, conversation_id=conv["id"],
    )
    context.record_context_receipt(built.receipt, conversation_id=conv["id"])
    expected = context.conversation_context_usage(conv["id"])
    with context._USAGE_LOCK:
        context._USAGE_BY_CONVERSATION.clear()
    assert context.conversation_context_usage(conv["id"]) == expected
    restored = conversations.get_conversation(conv["id"])
    assert restored["messages"] == original["messages"]
    assert restored["revision"] == original["revision"]
    assert restored["updated_at"] == original["updated_at"]


def test_evicted_chat_measurement_is_reloaded_from_its_own_store(chats):
    first = None
    for index in range(130):
        conv = conversations.create_conversation()
        result = context.build_context(
            [{"role": "user", "content": f"Fixture conversation {index}"}],
            provider="openai", model="gpt-4.1", conversation_id=conv["id"],
        )
        context.record_context_receipt(result.receipt, conversation_id=conv["id"])
        if first is None:
            first = conv["id"], context.conversation_context_usage(conv["id"])
    assert len(context._USAGE_BY_CONVERSATION) == 128
    assert context.conversation_context_usage(first[0]) == first[1]


def test_old_native_chats_restore_real_usage_without_any_native_request(chats, monkeypatch):
    client = AppServerClient(base_dir=chats / "native")
    monkeypatch.setattr(client, "start", lambda: pytest.fail("Counter restoration must not start the runtime"))
    provider = ChatGPTAccountProvider(client)
    ids = []
    for thread, used in [("fixture-thread-a", 100_000), ("fixture-thread-b", 42_000)]:
        conv = conversations.create_conversation()
        conversations.add_message(conv["id"], "user", "Only a small public transcript.")
        _legacy_binding(conv["id"], thread)
        _native_log(client, thread, used=used, header_size=6000)
        ids.append(conv["id"])
    for index in [0, 1, 0]:
        usage = provider.conversation_context_usage(ids[index], model="fixture-model")
        assert usage["used"] == [100_000, 42_000][index]
        assert usage["maximum"] == 258_400
        assert usage["estimated"] is False
        assert usage["restored"] is True
    assert client._process is None


def test_native_metadata_cache_refreshes_after_a_new_measurement(chats):
    client = AppServerClient(base_dir=chats / "native")
    path = _native_log(client, "fixture-thread")
    assert client.saved_thread_context_usage("fixture-thread")["used"] == 100_000
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 112_345},
                "total_token_usage": {"total_tokens": 999_999},
                "model_context_window": 258_400,
            },
        }}) + "\n")
    assert client.saved_thread_context_usage("fixture-thread")["used"] == 112_345


def test_native_log_scope_and_compaction_invalidation(chats):
    client = AppServerClient(base_dir=chats / "native")
    conv = conversations.create_conversation()
    _legacy_binding(conv["id"], "fixture-thread")
    path = _native_log(client, "fixture-thread")
    provider = ChatGPTAccountProvider(client)
    assert provider.conversation_context_usage(conv["id"], model="different-model") is None
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "compacted", "payload": {"message": "private fixture"}}) + "\n")
    assert provider.conversation_context_usage(conv["id"], model="fixture-model") is None
    rows = path.read_text().splitlines()
    first = json.loads(rows[0])
    first["payload"]["id"] = "other-thread"
    path.write_text(json.dumps(first) + "\n" + "\n".join(rows[1:]) + "\n", encoding="utf-8")
    assert client.saved_thread_context_usage("fixture-thread") is None
    assert client.saved_thread_context_usage("../fixture-thread") is None


class _MeasuredRuntime:
    generation = 1
    account_generation = 0

    def __init__(self, *, measured=True):
        self.measured = measured
        self.starts = 0

    def start(self):
        self.starts += 1

    def get_account_status(self):
        return {"connected": True, "account": {"email": "fixture@example.invalid"}}

    def start_or_resume_conversation(self, **kwargs):
        return kwargs["thread_id"] or "fixture-thread"

    def run_turn(self, **kwargs):
        kwargs["on_attempt_registered"]()
        return TurnResult("Fixture reply", kwargs["thread_id"], "native-turn", "completed")

    def thread_context_usage(self, thread_id):
        return {"used": 100_000, "maximum": 258_400, "estimated": False, "compacting": False} if self.measured else None


def test_native_usage_is_durable_after_the_turn(chats):
    conv = conversations.create_conversation()
    runtime = _MeasuredRuntime()
    ChatGPTAccountProvider(runtime).complete(
        conversation_id=conv["id"], model="fixture-model",
        messages=[{"role": "user", "content": "Fixture request"}],
    )
    binding = conversations.get_conversation(conv["id"])["_chatgpt_account_thread"]
    assert binding["context_usage"]["used"] == 100_000
    new_runtime = _MeasuredRuntime(measured=False)
    restored = ChatGPTAccountProvider(new_runtime).conversation_context_usage(conv["id"], model="fixture-model")
    assert restored["used"] == 100_000
    assert new_runtime.starts == 0


def _history(conv_id):
    for index in range(20):
        conversations.add_message(conv_id, "user" if index % 2 == 0 else "assistant", f"Historical text {index}. " * 100)


def test_compaction_journals_only_an_application_notice_and_keeps_original_turns(chats):
    from osia.agent import _sanitize_persisted_history_message
    from osia.server.server import _conversation_history_frame
    conv = conversations.create_conversation()
    _history(conv["id"])
    original = copy.deepcopy(conversations.get_conversation(conv["id"])["messages"])
    progress = []
    result = context.compact_conversation_context(
        conv["id"], "openai", "gpt-4.1", request_id="compact_fixture_request",
        on_started=lambda: progress.append(_conversation_history_frame(conversations.get_conversation(conv["id"]))),
    )
    assert progress[0]["context_compacting"] is True
    final = conversations.get_conversation(conv["id"])
    assert final["messages"][:-1] == original
    notice = final["messages"][-1]
    assert context.context_compaction_notice(notice, locale="fr")["content"] == (
        "Contexte compacté." if result["changed"] else "Le contexte est déjà court."
    )
    assert _sanitize_persisted_history_message(notice) is None
    wire = context.build_context(
        [{"role": "system", "content": "Keep these real instructions"}, *final["messages"]],
        provider="openai", model="gpt-4.1", conversation_id=conv["id"],
    ).messages
    assert all("_context_compaction_state" not in message for message in wire)
    assert not conversations.context_compaction_active(final)
    public = _conversation_history_frame(final)
    assert public["context_compacting"] is False
    assert public["messages"][-1]["role"] == "system"
    assert public["messages"][-1]["context_compaction_request_id"] == "compact_fixture_request"


def test_native_unavailable_never_announces_success(chats, monkeypatch):
    from osia.chatgpt_account import provider as account_provider
    conv = conversations.create_conversation()
    _history(conv["id"])
    _legacy_binding(conv["id"], "fixture-thread")
    monkeypatch.setattr(account_provider, "get_account_provider", lambda: SimpleNamespace(compact_conversation=lambda *args: False))
    with pytest.raises(context.ContextBuildError, match="native_compaction_unavailable"):
        context.compact_conversation_context(conv["id"], "chatgpt_account", "fixture-model")
    final = conversations.get_conversation(conv["id"])
    assert final["messages"][-1]["_context_compaction_state"] == "failed"
    assert not conversations.context_compaction_active(final)
    assert not conversations.conversation_context_compacted(conv["id"])


def test_busy_compaction_does_not_claim_success_and_does_not_prevent_a_fresh_request(chats):
    conv = conversations.create_conversation()
    _history(conv["id"])
    assert conversations.claim_background_execution(conv["id"], "fixture-other-owner")
    before = copy.deepcopy(conversations.get_conversation(conv["id"])["messages"])
    with pytest.raises(context.ContextBuildError, match="conversation_busy"):
        context.compact_conversation_context(conv["id"], "openai", "gpt-4.1")
    assert conversations.get_conversation(conv["id"])["messages"] == before
    assert conversations.release_background_execution(conv["id"], "fixture-other-owner")
    context.compact_conversation_context(conv["id"], "openai", "gpt-4.1")
    context.compact_conversation_context(conv["id"], "openai", "gpt-4.1")
    notes = [m for m in conversations.get_conversation(conv["id"])["messages"] if context.context_compaction_notice(m)]
    assert len(notes) == 2
    assert notes[0]["_message_id"] != notes[1]["_message_id"]


def _main_function(name):
    source = Path(__file__).resolve().parents[1] / "osia/main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wake = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "wake_mode")
    function = next(n for n in wake.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.Module(body=[function], type_ignores=[])


def test_first_meter_refresh_uses_selected_model_and_restored_native_context(chats, monkeypatch):
    from osia.chatgpt_account import provider as account_provider
    client = AppServerClient(base_dir=chats / "native")
    conv = conversations.create_conversation()
    conversations.add_message(conv["id"], "user", "A short public transcript")
    _legacy_binding(conv["id"], "fixture-thread")
    _native_log(client, "fixture-thread")
    native = ChatGPTAccountProvider(client)
    monkeypatch.setattr(account_provider, "get_account_provider", lambda: native)
    values = []
    popup = SimpleNamespace(
        isVisible=lambda: True, PAGE_CHAT=0, _stack=SimpleNamespace(currentIndex=lambda: 0),
        _chat=SimpleNamespace(
            set_context_compacting=lambda value: None,
            sync_context_notices=lambda value: None,
            _context_meter=SimpleNamespace(set_usage=values.append),
        ),
    )
    namespace = {
        "popup": popup, "active_conv": conv, "conversations": conversations,
        "agent": SimpleNamespace(provider="ollama", model="fixture-default"),
        "_active_chat_turn_for_conversation": lambda conv_id: {},
        "load_settings": lambda: {"selected_model_provider": "chatgpt_account", "selected_model": "fixture-model"},
        "pending_compactions": set(), "meter_cache": {},
    }
    exec(compile(_main_function("refresh_context_meter"), "<meter regression>", "exec"), namespace)
    namespace["refresh_context_meter"]()
    assert values[-1]["used"] == 100_000
    assert values[-1]["estimated"] is False
    assert client._process is None


def test_compaction_callback_can_finish_after_returning_to_same_chat_but_never_another_chat():
    calls = []
    namespace = {
        "active_conv": {"id": "a"}, "selection_epoch": [10], "pending_compactions": {"a"},
        "refresh_context_meter": lambda: calls.append("refresh"),
        "popup": SimpleNamespace(
            show_conversation_busy_notice=lambda: calls.append("busy"),
            _chat=SimpleNamespace(show_notice=lambda value: calls.append(value)),
        ),
        "i18n": SimpleNamespace(get_language=lambda: "fr"),
    }
    exec(compile(_main_function("handle_context_compaction_ready"), "<compaction regression>", "exec"), namespace)
    namespace["handle_context_compaction_ready"](1, "a", {"changed": True})
    assert calls == ["refresh"]
    calls.clear()
    namespace["active_conv"] = {"id": "b"}
    namespace["handle_context_compaction_ready"](1, "a", {"changed": True})
    assert calls == []


def test_corrupt_or_foreign_snapshot_does_not_mix_chats(chats):
    first = conversations.create_conversation()
    second = conversations.create_conversation()
    saved = {"used": 100_000, "maximum": 258_400, "estimated": False, "provider": "openai", "model": "gpt-4.1"}
    assert conversations.store_context_usage(second["id"], saved)
    assert context.conversation_context_usage(first["id"], conversation=conversations.get_conversation(second["id"])) is None
    assert not conversations.store_context_usage(first["id"], {**saved, "model": {"private": "not an identifier"}})
    assert context.context_compaction_notice({
        "role": "system", "_context_compaction_state": {}, "_context_compaction_id": "context_compaction_" + "a" * 32,
    }) is None


def test_desktop_progress_and_completion_are_chat_notices_not_model_bubbles(chats, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication, QLabel
    from osia import i18n
    from osia.gui.main_popup import _ChatPage
    app = QApplication.instance() or QApplication([])
    previous = i18n.get_language()
    i18n.set_language("fr", persist=False)
    page = _ChatPage()
    page.resize(440, 560)
    try:
        page.set_messages([{"role": "system", "content": "PRIVATE INSTRUCTIONS MUST NOT APPEAR"}])
        page.show()
        page.set_context_compacting(True)
        app.processEvents()
        assert page._compaction_progress.isVisible()
        assert page._compaction_progress.text() == "Compaction en cours…"
        notice = {
            "role": "system", "content": "PRIVATE BODY MUST NOT APPEAR",
            "_context_compaction_id": "context_compaction_" + "a" * 32,
            "_context_compaction_state": "completed", "_message_id": "fixture-notice",
        }
        page.set_context_compacting(False)
        page.sync_context_notices([notice, notice])
        labels = [label.text() for label in page.findChildren(QLabel)]
        assert labels.count("Contexte compacté.") == 1
        assert all("PRIVATE" not in text for text in labels)
        assert page._compaction_progress is None
        page.set_messages([notice])
        assert len(page._context_notices) == 1
        page._context_meter.set_usage({"used": 100_000, "maximum": 258_400, "estimated": False, "restored": True})
        assert "100" in page._context_meter.toolTip() and "dernière mesure connue" in page._context_meter.toolTip()
    finally:
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        i18n.set_language(previous, persist=False)


def test_server_projects_progress_then_completion_before_the_exact_acknowledgement(chats):
    import asyncio
    from osia.server import server
    conv = conversations.create_conversation()
    _history(conv["id"])
    frames = []

    async def send(frame):
        frames.append(copy.deepcopy(frame))

    source = Path(__file__).resolve().parents[1] / "osia/server/server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "respond_context_compaction")
    namespace = {
        "conv_id": conv["id"], "operation_id": "compact_fixture_ack",
        "asyncio": asyncio, "conversations": conversations,
        "_conversation_history_frame": server._conversation_history_frame,
        "_send_conversation_frame": send,
        "_resolve_chat_route_binding": lambda: {"provider_id": "openai", "model_id": "gpt-4.1"},
        "out": SimpleNamespace(send_json=send),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<context socket regression>", "exec"), namespace)
    asyncio.run(namespace["respond_context_compaction"]())
    progress = next(frame for frame in frames if frame.get("context_compacting") is True)
    completion = next(frame for frame in frames if frame.get("type") == "conversation_history" and frame.get("context_compacting") is False)
    assert progress["context_compaction_update"] is True
    assert completion["context_compaction_update"] is True
    assert completion["messages"][-1]["context_compaction_request_id"] == "compact_fixture_ack"
    assert frames[-1]["operation_id"] == "compact_fixture_ack"
    assert frames[-1]["compact_conversation_id"] == conv["id"]
    assert "error" not in frames[-1] and not frames[-1]["compact_error"]
