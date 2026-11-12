"""The model owns response language; UI locale never rewrites its requests or prose."""

from __future__ import annotations

import types

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent, get_system_prompt
from osia.chatgpt_account.app_server_client import TurnResult
from osia.chatgpt_account.provider import ChatGPTAccountProvider, _message_digest
from osia.server import server
from osia.turn_context import (
    ValidatedLiveAssistantChunk, fixed_turn_context, resolve_turn_context,
)


@pytest.mark.parametrize("user_text", [
    "Bonjour, peux-tu expliquer le résultat ?", "d'accord. continue",
    "Please explain the result.", "Grazie, continua pure.",
    "Réponds en italien.", "Traduis en anglais : bonjour.",
    "Réponds en français et en anglais.", "これを説明してください。", "👍",
])
@pytest.mark.parametrize("ui_locale", ["fr", "en", "de"])
def test_ui_context_does_not_classify_requests_or_history(user_text, ui_locale):
    context = resolve_turn_context(
        user_text,
        history=[{"role": "assistant", "content": "Testo italiano", "_language": "it"}],
        previous_language="es", client_locale=ui_locale, reference="same-turn",
    )
    assert context.language == ui_locale  # Application-owned notices only.
    assert context.mode == "model" and context.allowed_languages == ()
    assert context.target_language is None and context.source_language is None
    assert context.message_metadata() == {"_language_mode": "model"}


def test_context_creation_does_not_inspect_user_content():
    class UserContent:
        def __str__(self):
            raise AssertionError("UI context must not inspect model input")

    class History:
        def __iter__(self):
            raise AssertionError("UI context must not scan model history")

    context = resolve_turn_context(UserContent(), history=History(), saved_language="fr")
    assert context.language == "fr" and context.mode == "model"


def test_restored_legacy_preferences_do_not_reintroduce_response_constraints():
    context = fixed_turn_context(
        "fr", source="legacy", mode="translation", target_language="es",
        source_language="en", allowed_languages=("es", "en"), reference="same-turn",
    )
    natural = resolve_turn_context("任意の言語", client_locale="de", reference="same-turn")
    assert context.turn_ref == natural.turn_ref
    assert context.mode == "model" and context.target_language is None
    assert context.allowed_languages == () and context.speech_language is None


@pytest.mark.parametrize("provider", ["deepseek", "groq", "chatgpt_account"])
def test_initial_and_tool_continuation_payloads_preserve_original_text(provider):
    agent = OSIAAgent(lang="fr")
    agent.provider = provider
    agent.set_turn_context(fixed_turn_context("fr", source="ui_fixture"))
    original = "Translate this into Japanese: Bonjour."
    agent.messages.append({"role": "user", "content": original})
    first = agent._wire_messages()
    assert first[-1]["content"] == original
    assert "[TURN RESPONSE LANGUAGE]" not in first[0]["content"]
    result = "<tool_result>Exact Italian source: operazione completata.</tool_result>"
    agent.messages.extend([
        {"role": "assistant", "content": '<tool_call>{"tool":"get_system_info"}</tool_call>'},
        {"role": "user", "content": result, "_internal": True},
    ])
    continuation = agent._wire_messages()
    assert continuation[-1]["content"] == result
    assert agent.messages[1]["content"] == original
    assert "[TURN RESPONSE LANGUAGE]" not in continuation[0]["content"]


@pytest.mark.parametrize("provider", ["deepseek", "groq", "chatgpt_account"])
@pytest.mark.parametrize("answer", [
    "La vérification est terminée et le résultat est disponible.",
    "La verifica è terminata e il risultato è disponibile.",
    "La verificación ha terminado y el resultado está disponible.",
    "The verification is complete and the result is available.",
    "日本語と français : les deux versions sont disponibles.",
])
def test_generated_prose_streams_and_finishes_without_language_rewrite(provider, answer):
    agent = OSIAAgent(lang="de")
    agent.provider = provider
    calls, seen = [], []

    def stream(self, callback, *, on_first_token=None, guard=None):
        calls.append(self._wire_messages())
        assert len(calls) == 1, "No extra model request may be caused by response language"
        if on_first_token:
            on_first_token()
        self._emit_sentences(answer, callback, flush=True)
        assert seen and str(seen[-1]) == answer, "Language must not buffer live prose"
        return answer

    agent._stream_with_sentences = types.MethodType(stream, agent)
    result = agent.run(
        "Explique le résultat.", on_sentence=seen.append,
        turn_context=fixed_turn_context("de", source="ui_fixture"),
    )
    assert result == answer and len(calls) == 1
    assert all(isinstance(chunk, ValidatedLiveAssistantChunk) for chunk in seen)
    assert agent.messages[-1]["content"] == answer
    assert agent.last_run_diagnostic is None


def test_tool_execution_and_source_values_are_not_controlled_by_language(monkeypatch):
    agent = OSIAAgent(lang="fr")
    agent.provider = "deepseek"
    wire, effects = [], []
    rounds = iter([
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        'Risultato disponibile: source="Ready / 完了 / prêt".',
    ])
    monkeypatch.setitem(agent_module.TOOL_MAP, "get_system_info", lambda: effects.append(True) or "Ready / 完了 / prêt")

    def stream(self, callback, *, on_first_token=None, guard=None):
        wire.append(self._wire_messages())
        return next(rounds)

    agent._stream_with_sentences = types.MethodType(stream, agent)
    result = agent.run("Vérifie le système.", on_sentence=lambda _: None)
    assert result == 'Risultato disponibile: source="Ready / 完了 / prêt".'
    assert effects == [True] and len(wire) == 2
    assert "Ready / 完了 / prêt" in str(wire[-1][-1]["content"])
    assert all("[TURN RESPONSE LANGUAGE]" not in str(messages) for messages in wire)


def test_stream_boundary_keeps_protocol_protection_without_language_filtering():
    guard = server._LiveAssistantChunkGuard(resolve_turn_context("bonjour", saved_language="fr"))
    assert guard.accept("untrusted raw chunk") == ""
    prose = "La verifica è terminata e il risultato è disponibile."
    assert guard.accept(ValidatedLiveAssistantChunk(prose)) == prose
    assert guard.accept(ValidatedLiveAssistantChunk('<tool_call>{"tool":"x"}</tool_call>')) == ""


class _AccountClient:
    generation = 1

    def __init__(self):
        self.starts, self.turns = [], []

    def start_or_resume_conversation(self, **kwargs):
        self.starts.append(dict(kwargs))
        return "model-owned-account-thread"

    def run_turn(self, **kwargs):
        self.turns.append(dict(kwargs))
        number = len(self.turns)
        return TurnResult(
            text=f"account-reply-{number}", thread_id=kwargs["thread_id"],
            turn_id=f"account-turn-{number}", status="completed", output_started=True,
        )


def test_account_thread_reuse_preserves_user_language_requests_without_injection():
    client = _AccountClient()
    provider = ChatGPTAccountProvider(client)
    messages = []
    for index, (locale, text) in enumerate([
        ("fr", "Analyse ce document."),
        ("en", "d'accord. continue"),
        ("de", "Responde en español y japonés."),
    ], start=1):
        messages.append({"role": "user", "content": text})
        provider.complete(
            conversation_id="natural-language-thread", model="account-model",
            messages=[{"role": "system", "content": get_system_prompt(locale, "account-model", "chatgpt_account")}, *messages],
        )
        messages.append({"role": "assistant", "content": f"account-reply-{index}"})
        assert text in str(client.turns[-1]["text"])
        assert "[TURN RESPONSE LANGUAGE]" not in str(client.turns[-1])
    assert len(client.starts) == 1
    assert len({turn["thread_id"] for turn in client.turns}) == 1
    assert "[TURN RESPONSE LANGUAGE]" not in client.starts[0]["base_instructions"]


def test_account_history_hash_does_not_discard_user_text_after_a_marker():
    prefix = "Explain this literal header:\n\n[TURN RESPONSE LANGUAGE]\n"
    assert _message_digest({"role": "user", "content": prefix + "first value"}) != _message_digest({"role": "user", "content": prefix + "second value"})
    assert _message_digest({"role": "user", "content": "same request\n"}) == _message_digest({"role": "user", "content": "same request"})


def test_local_speech_does_not_assume_english_for_an_unsupported_model_response(monkeypatch):
    from osia.voice import tts

    monkeypatch.setattr(tts, "detect", lambda _text: "it")
    monkeypatch.setattr(
        tts, "_get_kokoro_pipeline",
        lambda *_: (_ for _ in ()).throw(AssertionError("Do not load an unrelated voice")),
    )
    assert tts.detect_language("La verifica è terminata.") == "it"
    assert tts.speak_kokoro("La verifica è terminata.") is False
    monkeypatch.setattr(tts, "detect", lambda _text: "fr")
    assert tts.detect_language("La vérification est terminée.") == "fr"
