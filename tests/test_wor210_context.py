"""Focused WOR-210 canonical context, compaction and recovery proofs."""

from __future__ import annotations

import copy
import json
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osia import conversations, document_artifacts, document_sources
from osia.agent import (
    OSIAAgent,
    SYSTEM_PROMPT_CORE,
    _latest_persisted_document_references,
)
from osia.conversation_context import (
    ContextBuildError,
    build_context,
    estimate_messages_tokens,
)
from osia.main import _persist_agent_delta
from osia.server import server


def _artifact(suffix: str) -> dict:
    artifact_id = "doc_" + suffix * 32
    return {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "document_id": artifact_id,
        "document_type": "pdf",
        "page_count": 1,
        "page_methods": ["native"],
        "content_length": 80,
        "confidence": 1.0,
        "truncated": False,
        "warnings": [],
        "resumable": True,
        "expires_at": int(time.time()) + 3600,
    }


def _alternating_history(count: int, *, size: int = 60) -> list[dict]:
    messages = [{"role": "system", "content": "SYSTEM POLICY"}]
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        messages.append({
            "role": role,
            "content": f"turn-{index:03d} " + ("repetition " * size),
            "_conversation_id": "conv-long",
            "_message_id": f"message-{index:03d}",
        })
    return messages


def test_a_short_history_is_complete_ordered_and_origin_independent():
    stored = [
        {"role": "system", "content": "policy"},
        {"role": "developer", "content": "developer rule"},
        {"role": "user", "content": "EARLY_MARKER", "_message_id": "m1"},
        {"role": "assistant", "content": "ack", "_message_id": "m2"},
        {"role": "user", "content": "RECENT_MARKER", "_message_id": "m3"},
    ]

    pc = build_context(
        stored,
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        conversation_id="conv-short",
        turn_id="turn-short",
    )
    mobile = build_context(
        copy.deepcopy(stored),
        provider="deepseek",
        model="deepseek-flash",
        conversation_id="conv-short",
        turn_id="turn-short",
    )

    expected = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in stored
    ]
    assert pc.messages == expected
    assert mobile.messages == expected
    assert pc.receipt["compaction_strategy"] == "full_history"
    assert mobile.receipt["compaction_strategy"] == "full_history"
    assert pc.receipt["canonical_context_hash"] == mobile.receipt["canonical_context_hash"]
    assert [item["role"] for item in pc.messages] == [
        "system", "developer", "user", "assistant", "user",
    ]


def test_small_context_compacts_past_turns_without_dropping_an_active_tool_exchange():
    messages = _alternating_history(10, size=80)
    messages[0]["content"] = "System policy. " * 420
    call = {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "call_active", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }
    active = [
        {"role": "user", "content": "Current question"},
        call,
        {"role": "tool", "tool_call_id": "call_active", "content": "Current result"},
    ]
    result = build_context(
        messages + active, provider="ollama", model="test-model",
        conversation_id="conv-long", context_limit=8_192,
    )
    assert result.receipt["compaction_strategy"].startswith("deterministic_")
    assert result.messages[-3:] == active
    assert estimate_messages_tokens(result.messages) <= result.receipt["input_budget"]


def test_overflow_during_work_retains_latest_tool_exchange_and_objective():
    messages = [
        {"role": "system", "content": "System policy"},
        {"role": "user", "content": "Compare and add the missing projects."},
    ]
    for step in range(12):
        call_id = f"call_{step}"
        messages.extend([
            {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {
                "role": "tool", "tool_call_id": call_id,
                "content": f"PROJECT_{step} " + "observed evidence " * 67,
            },
        ])
    result = build_context(messages, provider="ollama", model="fixture", context_limit=8192)
    assert result.receipt["compaction_strategy"].startswith("deterministic_")
    assert result.messages[1] == messages[1]
    assert result.messages[-2:] == messages[-2:]
    assert "PROJECT_7" in result.messages[0]["content"]
    assert estimate_messages_tokens(result.messages) <= result.receipt["input_budget"]


def test_markdown_headings_survive_saving_and_reopening(monkeypatch, tmp_path):
    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    monkeypatch.setattr("osia.main._notify_mobile_conversation_changed", lambda *args, **kwargs: None)
    conv = conversations.create_conversation()
    question = "## Demande\nQuel est le résultat ?"
    answer = "## OSIA\nLe résultat est 42."
    previous = SimpleNamespace(messages=[
        {"role": "system", "content": SYSTEM_PROMPT_CORE},
        {"role": "user", "content": question},
        {"role": "assistant", "content": SYSTEM_PROMPT_CORE},
        {"role": "assistant", "content": answer},
    ])
    assert _persist_agent_delta(conv["id"], previous, 0)
    history = conversations.get_conversation(conv["id"])["messages"]
    assert [message["content"] for message in history] == [question, answer]

    completions = _CaptureCompletions()
    resumed = OSIAAgent(model="local-test", lang="fr")
    resumed.provider = "ollama"
    resumed.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    resumed.run("Peux-tu préciser ?", history=history, current_conversation_id=conv["id"])

    dialogue = [
        (message["role"], message["content"])
        for message in completions.calls[0]
        if message["role"] in {"user", "assistant"}
    ]
    assert dialogue[:2] == [("user", question), ("assistant", answer)]


def test_b_long_history_preserves_markers_constraints_and_recent_verbatim():
    messages = _alternating_history(62)
    messages[1]["content"] = (
        "EARLY_MARKER. Never change project ORION; the required color is cobalt."
    )
    messages[15]["content"] = "Project Atlas belongs to Alice."
    messages[31]["content"] = "MIDDLE_MARKER. The decision remains ORION."
    messages[-1]["content"] = "END_MARKER: answer using the early constraint."
    recent = [copy.deepcopy(item) for item in messages[-10:]]

    result = build_context(
        messages,
        provider="ollama",
        model="small-local",
        conversation_id="conv-long",
        turn_id="turn-long",
        context_limit=8192,
    )
    rendered = "\n".join(str(item.get("content") or "") for item in result.messages)

    assert result.receipt["compaction_strategy"].startswith("deterministic_")
    assert result.receipt["estimated_tokens_after"] <= result.receipt["input_budget"]
    assert estimate_messages_tokens(result.messages) <= result.receipt["input_budget"]
    assert "EARLY_MARKER" in rendered
    assert "MIDDLE_MARKER" in rendered
    assert "END_MARKER" in rendered
    assert "ORION" in rendered and "cobalt" in rendered
    assert "Atlas" in rendered and "Alice" in rendered
    for item in recent:
        assert any(
            candidate.get("role") == item["role"]
            and candidate.get("content") == item["content"]
            for candidate in result.messages
        )
    assert len(result.messages) < len(messages)


def test_c_active_tool_result_and_security_confirmation_are_never_dropped():
    messages = _alternating_history(48)
    messages[3]["content"] = "I approve the security confirmation SECURITY_OK."
    messages.extend([
        {"role": "user", "content": "Run the final lookup."},
        {"role": "assistant", "content": "<tool_call>lookup</tool_call>"},
        {"role": "user", "content": "<tool_result>ACTIVE_RESULT_42</tool_result>"},
    ])
    result = build_context(
        messages,
        provider="ollama",
        model="small-local",
        conversation_id="conv-long",
        context_limit=8192,
    )
    rendered = "\n".join(str(item.get("content") or "") for item in result.messages)

    assert "ACTIVE_RESULT_42" in rendered
    assert "SECURITY_OK" in rendered
    assert result.receipt["included_tool_result_reference_count"] >= 1


class _CaptureCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs["messages"]))
        message = SimpleNamespace(content="Réponse factuelle.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_c_same_document_reference_survives_restart_without_unrelated_artifact():
    old_ref = _artifact("a")
    current_ref = _artifact("b")
    history = [
        {"role": "user", "content": "ancien document", "_conversation_id": "conv-doc"},
        {"role": "assistant", "content": "ancien", "_artifact_refs": [old_ref],
         "_conversation_id": "conv-doc"},
        {"role": "user", "content": "nouveau document", "_conversation_id": "conv-doc"},
        {"role": "assistant", "content": "nouveau", "_artifact_refs": [current_ref],
         "_conversation_id": "conv-doc"},
    ]
    latest = _latest_persisted_document_references(history, "conv-doc")
    assert [item["document_id"] for item in latest] == [current_ref["document_id"]]

    completions = _CaptureCompletions()
    agent = OSIAAgent(model="local-test", lang="fr")
    agent.provider = "ollama"
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reply = agent.run(
        "Que dit ce même document ?",
        history=copy.deepcopy(history),
        current_conversation_id="conv-doc",
    )
    wire = json.dumps(completions.calls[0], ensure_ascii=False)

    assert reply == "Réponse factuelle."
    assert current_ref["document_id"] in wire
    assert old_ref["document_id"] not in wire
    assert "_artifact_refs" not in wire


def test_c_desktop_document_turn_persists_visible_text_and_opaque_reference():
    ref = _artifact("c")
    agent = SimpleNamespace(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "C:\\private\\source.pdf", "_document_private": True},
            {"role": "assistant", "content": "Résumé sûr.", "_document_private": True},
        ],
        last_run_artifacts=[ref],
        turn_context=SimpleNamespace(
            language="fr", mode="single", allowed_languages=("fr",),
        ),
    )
    original_dir = conversations.CONVERSATIONS_DIR
    with tempfile.TemporaryDirectory(prefix="osia-wor210-") as temp_dir:
        conversations.CONVERSATIONS_DIR = Path(temp_dir)
        try:
            conv = conversations.create_conversation()
            _persist_agent_delta(
                conv["id"], agent, 0, user_display_text="source.pdf",
            )
            stored = conversations.get_conversation(conv["id"])
        finally:
            conversations.CONVERSATIONS_DIR = original_dir

    saved = (stored or {}).get("messages") or []
    assert [item["content"] for item in saved] == ["source.pdf", "Résumé sûr."]
    assert saved[-1]["_artifact_refs"][0]["document_id"] == ref["document_id"]
    assert "private" not in json.dumps(saved).lower()


def test_d_model_change_recomputes_budget_and_provider_payload_is_equivalent():
    long_messages = _alternating_history(80, size=260)
    agent = OSIAAgent(model="small-local", lang="en")
    agent.messages = copy.deepcopy(long_messages)
    agent._chatgpt_conversation_id = "conv-long"
    agent.provider = "ollama"
    small_wire = agent._wire_messages()
    small_receipt = copy.deepcopy(agent.last_context_receipt)

    agent.provider = "deepseek"
    agent.model = "deepseek-flash"
    large_wire = agent._wire_messages()
    large_receipt = copy.deepcopy(agent.last_context_receipt)

    assert small_receipt["compaction_strategy"].startswith("deterministic_")
    assert large_receipt["compaction_strategy"] == "full_history"
    assert len(small_wire) < len(large_wire)
    assert small_receipt["context_limit"] < large_receipt["context_limit"]

    short = long_messages[:5]
    account = build_context(
        short, provider="chatgpt_account", model="gpt-5.6-terra",
        conversation_id="conv-long",
    )
    api = build_context(
        copy.deepcopy(short), provider="deepseek", model="deepseek-flash",
        conversation_id="conv-long",
    )
    assert account.messages == api.messages
    assert account.receipt["canonical_context_hash"] == api.receipt["canonical_context_hash"]


def test_e_isolation_deduplication_recovery_and_safe_failures():
    mismatch = [{
        "role": "user", "content": "private-a", "_conversation_id": "conv-a",
    }]
    with pytest.raises(ContextBuildError, match="conversation_scope_mismatch"):
        build_context(
            mismatch, provider="ollama", model="local",
            conversation_id="conv-b",
        )
    with pytest.raises(ContextBuildError, match="conversation_scope_mismatch"):
        _latest_persisted_document_references(mismatch, "conv-b")

    duplicate = [
        {"role": "user", "content": "same", "_message_id": "duplicate-id"},
        {"role": "user", "content": "same", "_message_id": "duplicate-id"},
    ]
    deduped = build_context(
        duplicate, provider="ollama", model="local", conversation_id="conv-dedup",
    )
    assert len(deduped.messages) == 1
    assert deduped.receipt["duplicates_removed"] == 1

    conflict = copy.deepcopy(duplicate)
    conflict[1]["content"] = "different"
    with pytest.raises(ContextBuildError, match="message_identity_conflict"):
        build_context(conflict, provider="ollama", model="local")
    with pytest.raises(ContextBuildError, match="malformed_history_message"):
        build_context(["not-a-message"], provider="ollama", model="local")
    with patch("osia.conversation_context.MAX_MESSAGE_CHARS", 4):
        with pytest.raises(ContextBuildError, match="oversized_message"):
            build_context(
                [{"role": "user", "content": "oversized"}],
                provider="ollama", model="local",
            )

    original_dir = conversations.CONVERSATIONS_DIR
    with tempfile.TemporaryDirectory(prefix="osia-wor210-restart-") as temp_dir:
        conversations.CONVERSATIONS_DIR = Path(temp_dir)
        try:
            conv = conversations.create_conversation()
            conversations.add_message(conv["id"], "user", "restart marker")
            conversations.add_message(conv["id"], "assistant", "restart reply")
            before_restart = conversations.get_conversation(conv["id"])["messages"]
            after_restart = conversations.get_conversation(conv["id"])["messages"]
        finally:
            conversations.CONVERSATIONS_DIR = original_dir
    first = build_context(
        before_restart, provider="ollama", model="local", conversation_id=conv["id"],
    )
    second = build_context(
        after_restart, provider="ollama", model="local", conversation_id=conv["id"],
    )
    assert first.messages == second.messages
    assert first.receipt["canonical_context_hash"] == second.receipt["canonical_context_hash"]


def test_e_receipt_contains_metadata_only_and_server_persists_artifact_reference():
    secret = "SECRET_MESSAGE_TEXT C:\\private\\attachment.pdf password=never-log"
    result = build_context(
        [{"role": "system", "content": "PRIVATE SYSTEM PROMPT"},
         {"role": "user", "content": secret, "_message_id": "sensitive-id"}],
        provider="ollama", model="local", conversation_id="conv-safe",
        turn_id="turn-safe",
    )
    receipt_text = json.dumps(result.receipt, sort_keys=True)
    assert "SECRET_MESSAGE_TEXT" not in receipt_text
    assert "PRIVATE SYSTEM PROMPT" not in receipt_text
    assert "private" not in receipt_text.lower()
    assert "password" not in receipt_text.lower()
    assert "sensitive-id" not in receipt_text

    ref = _artifact("d")
    meta = {
        "request_id": "req-safe",
        "client_request_id": "req-safe",
        "turn_id": "turn-safe",
        "user_message_id": "user-safe",
        "assistant_message_id": "assistant-safe",
        "_artifact_refs": [ref],
    }
    original_dir = conversations.CONVERSATIONS_DIR
    with tempfile.TemporaryDirectory(prefix="osia-wor210-server-") as temp_dir:
        conversations.CONVERSATIONS_DIR = Path(temp_dir)
        try:
            conv = conversations.create_conversation()
            assert server._persist_accepted_user_message(conv["id"], "question", meta)
            assert server._persist_completed_assistant_message(
                conv["id"], "answer", meta,
                {"event_id": "evt-response", "sequence": 1},
                {"event_id": "evt-terminal", "sequence": 2, "status": "done"},
            )
            stored = conversations.get_conversation(conv["id"])
        finally:
            conversations.CONVERSATIONS_DIR = original_dir
    assert stored["messages"][-1]["_artifact_refs"][0]["document_id"] == ref["document_id"]


def test_f_private_artifact_rehydrates_after_restart_without_source_reread(
    tmp_path, monkeypatch,
):
    conversation_id = "a" * 32
    wrong_conversation_id = "b" * 32
    now = [2_000_000_000.0]
    fact = "WOR210_PRIVATE_DOCUMENT_FACT"
    original_dir = conversations.CONVERSATIONS_DIR
    original_store = document_artifacts.get_store()
    conversations.CONVERSATIONS_DIR = tmp_path / "conversations"
    store_root = tmp_path / "artifacts"
    store = document_artifacts.ArtifactStore(store_root, clock=lambda: now[0])
    document_artifacts.set_store_for_testing(store)
    try:
        conversations.CONVERSATIONS_DIR.mkdir(parents=True)
        conversation = {
            "id": conversation_id,
            "title": "WOR-210 synthetic",
            "created_at": "2026-07-30T00:00:00",
            "updated_at": "2026-07-30T00:00:00",
            "messages": [],
        }
        conversations._write_atomic(
            conversations.CONVERSATIONS_DIR / f"{conversation_id}.json",
            conversation,
        )
        with document_artifacts.artifact_access_scope(conversation_id):
            reference = store.create(fact, {
                "document_type": "pdf",
                "page_count": 1,
                "page_methods": ["native"],
                "confidence": 1.0,
            })
        conversations.add_message(conversation_id, "user", "synthetic document")
        conversations.add_message(
            conversation_id,
            "assistant",
            "opaque document ready",
            metadata={"_artifact_refs": [reference]},
        )

        restarted_store = document_artifacts.ArtifactStore(
            store_root, clock=lambda: now[0],
        )
        document_artifacts.set_store_for_testing(restarted_store)
        monkeypatch.setattr(
            document_artifacts.ArtifactStore,
            "_creator_alive",
            staticmethod(lambda _metadata: False),
        )
        assert document_artifacts.cleanup_startup() == {"removed": 0}

        server_reloaded_store = document_artifacts.ArtifactStore(
            store_root, clock=lambda: now[0],
        )
        document_artifacts.set_store_for_testing(server_reloaded_store)
        with document_artifacts.artifact_access_scope(
            conversation_id, [reference["artifact_id"]],
        ):
            assert server_reloaded_store.inspect(reference["artifact_id"]) == reference
        with document_artifacts.artifact_access_scope(
            wrong_conversation_id, [reference["artifact_id"]],
        ):
            with pytest.raises(
                document_artifacts.ArtifactStoreError,
                match="unauthorized_artifact",
            ):
                server_reloaded_store.inspect(reference["artifact_id"])

        monkeypatch.setattr(
            document_sources.get_source_store(),
            "resolve",
            lambda *_args, **_kwargs: pytest.fail(
                "a valid persisted artifact reread its private source",
            ),
        )
        history = conversations.get_conversation(conversation_id)["messages"]
        provider_results = []
        for provider, model in (
            ("chatgpt_account", "gpt-5.6-terra"),
            ("deepseek", "deepseek-flash"),
        ):
            agent = OSIAAgent(model=model, lang="en")
            agent.provider = provider
            rounds = iter((
                '<tool_call>{"tool":"query_document","document_id":"'
                + reference["artifact_id"]
                + '","operation":"search","query":"WOR210_PRIVATE_DOCUMENT_FACT"}'
                + "</tool_call>",
                fact,
            ))

            def stream(current, callback, *, on_first_token=None, guard=None):
                content = next(rounds)
                if content == fact:
                    tool_results = "\n".join(
                        str(message.get("content") or "")
                        for message in current.messages
                        if str(message.get("content") or "").startswith("<tool_result>")
                    )
                    assert fact in tool_results
                callback(content)
                return content

            agent._stream_with_sentences = types.MethodType(stream, agent)
            provider_results.append(agent.run(
                "What fact is in this same document?",
                history=copy.deepcopy(history),
                current_conversation_id=conversation_id,
                on_sentence=lambda _text: None,
            ))
        assert provider_results == [fact, fact]

        long_history = _alternating_history(62)
        for message in long_history:
            if isinstance(message, dict):
                message["_conversation_id"] = conversation_id
        compacted = build_context(
            long_history + history,
            provider="ollama",
            model="small-local",
            conversation_id=conversation_id,
            context_limit=8192,
        )
        rendered_context = json.dumps(compacted.messages, sort_keys=True)
        rendered_receipt = json.dumps(compacted.receipt, sort_keys=True)
        assert reference["artifact_id"] not in rendered_context
        assert fact not in rendered_context
        assert fact not in rendered_receipt
        assert compacted.receipt["included_artifact_reference_count"] == 1

        malformed = dict(reference, document_type="invalid")
        assert document_artifacts.normalize_references([malformed, {"bad": True}]) == []
        now[0] = float(reference["expires_at"] + 1)
        with document_artifacts.artifact_access_scope(
            conversation_id, [reference["artifact_id"]],
        ):
            with pytest.raises(
                document_artifacts.ArtifactStoreError,
                match="expired_artifact",
            ):
                server_reloaded_store.inspect(reference["artifact_id"])
        assert server_reloaded_store.delete(reference["artifact_id"])
        with document_artifacts.artifact_access_scope(
            conversation_id, [reference["artifact_id"]],
        ):
            with pytest.raises(
                document_artifacts.ArtifactStoreError,
                match="missing_artifact",
            ):
                server_reloaded_store.inspect(reference["artifact_id"])
    finally:
        document_artifacts.set_store_for_testing(original_store)
        conversations.CONVERSATIONS_DIR = original_dir
