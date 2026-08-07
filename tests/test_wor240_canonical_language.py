"""Deterministic WOR-240 canonical-language regressions."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import types
from unittest.mock import patch

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent, get_system_prompt
from osia.autonomous import planner
from osia.server import server
from osia.turn_context import (
    fixed_turn_context,
    language_consistency_failure_text,
    language_instruction,
    localized_text,
    response_language_drift,
    resolve_turn_context,
)


def _history(language: str) -> list[dict]:
    return [
        {"role": "user", "content": "canonical", "_language": language},
        {"role": "assistant", "content": "canonical", "_language": language},
    ]


def _install_stream_rounds(agent: OSIAAgent, rounds, captured=None) -> None:
    remaining = iter(rounds)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        if captured is not None:
            captured.append(self._wire_messages())
        content, fragments = next(remaining)
        if on_first_token is not None:
            on_first_token()
        for fragment in fragments:
            callback(fragment)
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_reported_french_sequence_overrides_english_product_setting_once():
    history: list[dict] = []
    contexts = []

    for phrase in ("salut osia", "cava ?", "ça va ?", "ok", "continue"):
        context = resolve_turn_context(
            phrase,
            history=history,
            saved_language="en",
            reference=f"wor240-{len(contexts)}",
        )
        contexts.append(context)
        history.extend((
            {"role": "user", "content": phrase, "_language": context.language},
            {"role": "assistant", "content": "fixture", "_language": context.language},
        ))

    assert [context.target_language for context in contexts] == ["fr"] * 5
    assert [context.language for context in contexts] == ["fr"] * 5
    assert contexts[0].source == "current_request"
    assert all(context.source == "persisted_previous" for context in contexts[1:])
    with pytest.raises(FrozenInstanceError):
        contexts[0].target_language = "en"


@pytest.mark.parametrize("phrase", ["cava ?", "ça va ?", "ok", "continue"])
def test_new_conversation_ambiguous_french_turn_uses_client_locale(phrase):
    context = resolve_turn_context(
        phrase,
        history=[],
        client_locale="fr-FR",
        saved_language="en",
    )
    assert context.target_language == context.language == "fr"
    assert context.source == "client_locale"


def test_current_french_and_explicit_switches_beat_opposite_history():
    french = resolve_turn_context("salut osia", history=_history("en"))
    english = resolve_turn_context(
        "Please answer in English now.", history=_history("fr"),
    )
    back_to_french = resolve_turn_context(
        "Réponds de nouveau en français.", history=_history("en"),
    )

    assert (french.target_language, french.source) == ("fr", "current_request")
    assert (english.target_language, english.source) == ("en", "explicit_request")
    assert (back_to_french.target_language, back_to_french.source) == (
        "fr", "explicit_request",
    )


@pytest.mark.parametrize(
    "wrong_final",
    [
        "Hello! How can I help?",
        "I’m doing well, thank you! How are you?",
        "Sure.",
        "Okay.",
        "Yes.",
        "Done.",
        "Continuing now.",
    ],
)
def test_short_english_assistant_prose_is_rejected_for_french_turn(wrong_final):
    context = resolve_turn_context("salut osia", saved_language="en")
    assert response_language_drift(wrong_final, context) == "en"


def test_opaque_external_value_is_not_misclassified_as_assistant_prose():
    context = fixed_turn_context("fr", source="wor240-external-value")
    assert response_language_drift("WOR210_PRIVATE_DOCUMENT_FACT", context) is None


def test_valid_short_english_final_is_not_misclassified_as_italian():
    context = fixed_turn_context("en", source="wor240-short-english-final")
    assert response_language_drift("One canonical final response.", context) is None


def test_partial_english_stream_is_private_and_one_repaired_final_is_exported():
    context = resolve_turn_context("salut osia", saved_language="en")
    repaired = "Bonjour ! Comment puis-je vous aider ?"
    wire_rounds = []
    agent = OSIAAgent(lang="en")
    _install_stream_rounds(agent, [
        ("Hello! How can I help?", ["Hello!", "How can I help?"]),
        (repaired, ["Bonjour !", "Comment puis-je vous aider ?"]),
    ], wire_rounds)
    visible = []

    final = agent.run(
        "salut osia",
        history=[],
        on_sentence=visible.append,
        turn_context=context,
    )

    assert final == repaired
    assert visible == [repaired]
    assert len(wire_rounds) == 2
    assert any(
        message.get("role") == "assistant"
        and message.get("content") == "Hello! How can I help?"
        for message in wire_rounds[1]
    )
    assert wire_rounds[1][-1]["content"].rstrip().endswith(
        language_instruction(context)
    )
    assert [item["content"] for item in agent.exported_history()] == [
        "salut osia", repaired,
    ]


def test_failed_or_second_wrong_repair_returns_one_localized_typed_error():
    context = resolve_turn_context("salut osia", saved_language="en")
    expected = language_consistency_failure_text("fr")

    for repair in (RuntimeError("private provider failure"), "Okay."):
        agent = OSIAAgent(lang="en")
        calls = []

        def fake_stream(self, callback, *, on_first_token=None, guard=None):
            calls.append(self._wire_messages())
            if len(calls) == 1:
                return "Hello! How can I help?"
            if isinstance(repair, Exception):
                raise repair
            return repair

        agent._stream_with_sentences = types.MethodType(fake_stream, agent)
        visible = []
        final = agent.run(
            "salut osia", on_sentence=visible.append, turn_context=context,
        )

        assert len(calls) == 2
        assert final == expected
        assert visible == [expected]
        assert agent.last_run_diagnostic == {
            "stage": "response_generation",
            "error_category": "language_consistency_failure",
            "reason_code": "language_consistency_failure",
        }


def test_english_skill_and_spanish_tool_result_cannot_replay_effects_for_repair(
    monkeypatch,
):
    from osia import personality

    executions = []
    monkeypatch.setattr(
        personality,
        "load_enabled_skills_text",
        lambda: "English skill example: write a concise answer.",
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: executions.append("system") or (
            "Resultado externo: el servicio está listo. URL: https://example.test"
        ),
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    context = fixed_turn_context("fr", source="wor240-tool")
    agent = OSIAAgent(lang="fr")
    _install_stream_rounds(agent, [
        ('<tool_call>{"tool":"get_system_info"}</tool_call>', []),
        ("Okay.", ["Okay."]),
        (
            "Le service est prêt.\n"
            "Source: Resultado externo: el servicio está listo.\n"
            "URL: https://example.test",
            ["Le service est prêt."],
        ),
    ])
    visible = []

    final = agent.run(
        "Vérifie clairement le système.",
        on_sentence=visible.append,
        turn_context=context,
    )

    assert executions == ["system"]
    assert final.startswith("Le service est prêt.")
    assert "https://example.test" in final
    assert visible == [final]


@pytest.mark.parametrize("provider", ["deepseek", "groq", "chatgpt_account"])
def test_representative_provider_contract_uses_canonical_target(provider):
    context = resolve_turn_context("salut osia", history=_history("en"))
    prompt = get_system_prompt(
        "en", model="wor240-model", provider=provider, turn_context=context,
    )
    assert context.target_language == "fr"
    assert prompt.rstrip().endswith(language_instruction(context))


def test_autonomous_checkpoint_and_resume_keep_the_same_target_language():
    class Agent:
        def __init__(self):
            self.contexts = []
            self.messages = []

        def reset(self):
            return None

        def set_turn_context(self, context):
            self.contexts.append(context)

        def run(self, _prompt, **kwargs):
            assert kwargs["turn_context"].target_language == "fr"
            return (
                '{"phases":[{"name":"Phase",'
                '"tasks":[{"objective":"Continuer",'
                '"success_criterion":"La réponse existe"}]}]}'
            )

    context = resolve_turn_context("salut osia", saved_language="en")
    agent = Agent()
    state = planner.create_plan_state(
        "salut osia",
        "chatgpt_account",
        "wor240-model",
        agent,
        turn_context=context,
    )
    resumed = fixed_turn_context(
        state["language"],
        source="wor240-resume",
        mode=state["language_mode"],
        allowed_languages=state["allowed_languages"],
        source_language=state["source_language"],
        target_language=state["target_language"],
    )

    assert state["language"] == state["target_language"] == "fr"
    assert resumed.target_language == resumed.language == "fr"
    assert {item.target_language for item in agent.contexts} == {"fr"}


class _Channel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _turn_meta() -> dict:
    return {
        "conversation_id": "wor240-conversation",
        "request_id": "wor240-request",
        "client_request_id": "wor240-request",
        "turn_id": "wor240-turn",
        "user_message_id": "wor240-user",
        "assistant_message_id": "wor240-assistant",
        "client_locale": "fr-FR",
        "_event_sequence": 0,
        "persisted": False,
        "tts_enabled": False,
    }


def test_pc_mobile_projection_and_persistence_receive_only_repaired_french_final():
    repaired = "Bonjour ! Comment puis-je vous aider ?"
    agent = OSIAAgent(lang="en")
    _install_stream_rounds(agent, [
        ("Hello! How can I help?", ["Hello!", "How can I help?"]),
        (repaired, [repaired]),
    ])
    channel = _Channel()
    persisted = []
    meta = _turn_meta()

    with (
        patch("osia.config.load_settings", return_value={}),
        patch.object(server, "_agent_needs_model_apply", return_value=False),
        patch.object(server, "_persist_accepted_user_message", return_value=True),
        patch.object(
            server,
            "_persist_completed_assistant_message",
            side_effect=lambda _conv, text, *_args: persisted.append(text) or True,
        ),
    ):
        asyncio.run(
            server._run_agent_and_respond(
                channel,
                "salut osia",
                [],
                "wor240-conversation",
                get_agent=lambda _conv: agent,
                turn_meta=meta,
            )
        )

    assert meta["language"] == "fr"
    assert [
        frame["content"] for frame in channel.frames
        if frame["type"] == "response_chunk"
    ] == [repaired]
    assert [
        frame["content"] for frame in channel.frames
        if frame["type"] == "response"
    ] == [repaired]
    assert persisted == [repaired]
    assert all(
        frame.get("language") == "fr"
        for frame in channel.frames if frame.get("turn_id") == "wor240-turn"
    )


def test_tts_and_localized_failures_use_the_same_canonical_target(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "synthesize_to_mp3_bytes",
        lambda text, language=None: calls.append((text, language)) or b"mp3",
    )
    monkeypatch.setattr(server, "_tts_settings_signature", lambda: ("fixture",))
    context = resolve_turn_context("salut osia", saved_language="en")
    meta = _turn_meta()
    meta["language"] = context.target_language
    session = server._ResponseTTSSession(_Channel(), meta, 0.0)
    final = "Bonjour ! Comment puis-je vous aider ?"

    assert session._synthesize_with_stable_settings(final) == b"mp3"
    assert calls == [(final, "fr")]
    assert localized_text("provider_failure", context.target_language).startswith(
        "Je n’",
    )
