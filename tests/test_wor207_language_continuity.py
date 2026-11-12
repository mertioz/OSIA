import asyncio
import time
import types
from unittest.mock import patch

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent, get_system_prompt
from osia.server import server
from osia.tool_contracts import validation_error_text
from osia.turn_context import (
    fixed_turn_context,
    language_instruction,
    localized_text,
    resolve_turn_context,
)


def _history(language: str, text: str = "canonical") -> list[dict]:
    return [
        {"role": "user", "content": text, "_language": language},
        {"role": "assistant", "content": text, "_language": language},
    ]


def _context(language: str):
    return fixed_turn_context(
        language,
        source="wor207_fixture",
        reference=f"wor207-{language}",
    )


def test_consecutive_turns_and_clear_language_changes_are_per_turn():
    first_fr = resolve_turn_context("Bonjour, peux-tu expliquer ce résultat ?")
    second_fr = resolve_turn_context(
        "Merci, peux-tu préciser le deuxième point ?",
        history=_history(first_fr.language),
    )
    switch_en = resolve_turn_context(
        "Please continue in English and explain the second point.",
        history=_history(second_fr.language),
    )
    second_en = resolve_turn_context(
        "Thanks, can you make that more concise?",
        history=_history(switch_en.language),
    )
    switch_fr = resolve_turn_context(
        "Reviens en français et résume la conclusion.",
        history=_history(second_en.language),
    )

    assert [
        first_fr.language,
        second_fr.language,
        switch_en.language,
        second_en.language,
        switch_fr.language,
    ] == ["fr", "fr", "en", "en", "fr"]
    assert switch_en.source in {"explicit_request", "current_request"}
    assert switch_fr.source in {"explicit_request", "current_request"}


@pytest.mark.parametrize("previous", ["fr", "en"])
@pytest.mark.parametrize(
    "message",
    ["OK", "Yes", "Non", "Do it", "Encore", "Continue", "D'accord"],
)
def test_short_or_language_neutral_messages_preserve_previous_turn(
    previous, message,
):
    context = resolve_turn_context(message, history=_history(previous))
    assert context.language == previous
    assert context.source == "recent_user_context"


@pytest.mark.parametrize("message", [
    "d'accord. continue", "D’accord, continue", "ok, tu peux continuer",
])
def test_short_acknowledgement_does_not_override_french_history(message):
    context = resolve_turn_context(
        message,
        history=[
            {"role": "user", "content": "Peux-tu vérifier le résultat ?"},
            {"role": "assistant", "content": "Je continue la vérification.", "_language": "fr"},
        ],
    )
    assert context.language == "fr"
    assert context.source in {"recent_user_context", "current_request"}


def test_unannotated_acknowledgements_in_history_do_not_change_user_language():
    history = [
        {"role": "user", "content": "Peux-tu vérifier le résultat ?"},
        {"role": "assistant", "content": "Je continue la vérification.", "_language": "fr"},
        {"role": "user", "content": "d'accord. continue"},
        {"role": "assistant", "content": "Testo precedente in italiano.", "_language": "it"},
        {"role": "user", "content": "Synthetic English tool receipt", "_internal": True},
    ]
    context = resolve_turn_context("continue", history=history)
    assert (context.language, context.source) == ("fr", "recent_user_context")


def test_short_real_language_switches_and_translation_remain_available():
    history = [{"role": "user", "content": "Peux-tu vérifier le résultat ?", "_language": "fr"}]
    assert resolve_turn_context("Réponds en italien", history=history).language == "it"
    assert resolve_turn_context("Grazie, continua pure", history=history).language == "it"
    translated = resolve_turn_context("Traduis en anglais : « bonjour »", history=history)
    assert (translated.language, translated.mode) == ("en", "translation")
    bilingual = resolve_turn_context("Réponds en français et anglais", history=history)
    assert bilingual.mode == "multilingual" and set(bilingual.allowed_languages) == {"fr", "en"}


def test_quotes_code_technical_terms_and_tool_payloads_do_not_switch_language():
    french = resolve_turn_context(
        'Peux-tu résumer "Please reset the database" et vérifier `HTTP 500` ?',
        history=_history("fr"),
    )
    english = resolve_turn_context(
        "Can you explain `réponse_invalide` from the JSON API?",
        history=_history("en"),
    )
    assert french.language == "fr"
    assert english.language == "en"


@pytest.mark.parametrize("provider", ["deepseek", "groq", "chatgpt_account"])
@pytest.mark.parametrize("language", ["fr", "en"])
def test_representative_provider_prompts_end_with_the_turn_contract(
    provider, language,
):
    context = _context(language)
    prompt = get_system_prompt(
        language,
        model="wor207-deterministic-model",
        provider=provider,
        turn_context=context,
    )
    assert prompt.rstrip().endswith(language_instruction(context))


def _install_tool_rounds(agent, first_round: str, final: str, receipts: list[str]):
    rounds = iter((first_round, final))

    def fake_stream(self, _callback, *, on_first_token=None, guard=None):
        if on_first_token is not None:
            on_first_token()
        content = next(rounds)
        if content == final:
            receipts.append(str(self.messages[-1].get("content") or ""))
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


@pytest.mark.parametrize(
    ("language", "user_text", "final", "header"),
    [
        ("fr", "Vérifie le système.", "Le système répond correctement.", "Résultat de l’outil"),
        ("en", "Check the system.", "The system is responding correctly.", "Tool get_system_info result"),
    ],
)
def test_tool_success_receipt_keeps_target_language_and_raw_payload(
    monkeypatch, language, user_text, final, header,
):
    raw_payload = "RAW technical payload: service=ready"
    receipts = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP, "get_system_info", lambda: raw_payload,
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang=language)
    agent.provider = "deepseek"
    _install_tool_rounds(
        agent,
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        final,
        receipts,
    )

    assert agent.run(
        user_text, on_sentence=lambda _text: None, turn_context=_context(language),
    ) == final
    assert len(receipts) == 1
    assert header in receipts[0]
    assert raw_payload in receipts[0]
    assert receipts[0].rstrip().endswith(language_instruction(_context(language)))


@pytest.mark.parametrize(
    ("language", "user_text", "final", "banner"),
    [
        ("fr", "Utilise cet outil.", "L’outil demandé n’existe pas.", "ÉCHEC DE L’OUTIL"),
        ("en", "Use this tool.", "The requested tool does not exist.", "TOOL FAILED"),
    ],
)
def test_tool_failure_receipt_is_localized_without_exposing_arguments(
    language, user_text, final, banner,
):
    receipts = []
    agent = OSIAAgent(lang=language)
    agent.provider = "groq"
    _install_tool_rounds(
        agent,
        '<tool_call>{"tool":"wor207_missing_tool","secret":"PRIVATE"}</tool_call>',
        final,
        receipts,
    )

    assert agent.run(
        user_text, on_sentence=lambda _text: None, turn_context=_context(language),
    ) == final
    assert banner in receipts[0]
    assert localized_text(
        "unknown_tool_error", language, tool="wor207_missing_tool",
    ) in receipts[0]
    assert "PRIVATE" not in receipts[0]
    assert receipts[0].rstrip().endswith(language_instruction(_context(language)))


class _NativeActionRefusal(RuntimeError):
    native_action = True
    reformulatable_native_action = True


def test_controlled_native_action_refusal_uses_the_authoritative_language():
    agent = OSIAAgent(lang="fr")
    agent.provider = "chatgpt_account"
    wire_rounds = []

    def refuse(self, _callback, *, on_first_token=None, guard=None):
        wire_rounds.append(self._wire_messages())
        raise _NativeActionRefusal("PRIVATE ENGLISH PROVIDER DETAIL")

    agent._stream_with_sentences = types.MethodType(refuse, agent)
    final = agent.run(
        "Effectue cette action.",
        on_sentence=lambda _text: None,
        turn_context=_context("fr"),
    )

    assert len(wire_rounds) == 2
    assert final == localized_text("chatgpt_account.native_action_refused", "fr")
    assert "PRIVATE ENGLISH PROVIDER DETAIL" not in final
    assert wire_rounds[-1][-1]["content"].rstrip().endswith(
        language_instruction(_context("fr")),
    )


def test_provider_endpoint_fallback_preserves_language_and_prompt(monkeypatch):
    agent = OSIAAgent(lang="fr")
    agent.provider = "groq"
    prompts = []
    calls = 0

    def stream(self, _callback, *, on_first_token=None, guard=None):
        nonlocal calls
        calls += 1
        prompts.append(self._wire_messages()[0]["content"])
        if calls == 1:
            raise ConnectionError("synthetic endpoint failure")
        return "Le fournisseur de secours a répondu en français."

    def activate(self):
        self._active_base_url = "https://wor207.invalid/fallback"
        return True

    agent._stream_with_sentences = types.MethodType(stream, agent)
    agent._activate_fallback_endpoint = types.MethodType(activate, agent)
    monkeypatch.setattr(agent, "_disable_hierarchy_for_turn", True, raising=False)

    final = agent.run(
        "Peux-tu répondre à cette question ?",
        on_sentence=lambda _text: None,
        turn_context=_context("fr"),
    )

    assert calls == 2
    assert final == "Le fournisseur de secours a répondu en français."
    assert all(
        prompt.rstrip().endswith(language_instruction(_context("fr")))
        for prompt in prompts
    )


def test_supported_validation_receipts_are_localized():
    messages = {language: validation_error_text(language) for language in (
        "en", "fr", "zh", "ja", "de", "es",
    )}
    assert all(message.startswith("ERROR:") for message in messages.values())
    assert len(set(messages.values())) == len(messages)


def test_authorization_suspensions_keep_localized_context_codes():
    for language in ("en", "fr", "zh", "ja", "de", "es"):
        scheduled = localized_text(
            "scheduled_authorization_suspended",
            language,
            reason="authorization_revoked",
        )
        autonomous = localized_text(
            "autonomous_authorization_suspended",
            language,
            reason="suspended_scope_extension",
        )
        assert scheduled.startswith("ERROR:")
        assert autonomous.startswith("ERROR:")
        assert "authorization_revoked" in scheduled
        assert "suspended_scope_extension" in autonomous


class _Channel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _turn_meta(language: str, suffix: str) -> dict:
    return {
        "conversation_id": f"wor207-conversation-{suffix}",
        "request_id": f"wor207-request-{suffix}",
        "client_request_id": f"wor207-request-{suffix}",
        "turn_id": f"wor207-turn-{suffix}",
        "run_id": f"wor207-turn-{suffix}",
        "provider_id": "ollama",
        "model_id": "llama3",
        "effective_reasoning": "auto",
        "user_message_id": f"wor207-user-{suffix}",
        "assistant_message_id": f"wor207-assistant-{suffix}",
        "language": language,
        "_event_sequence": 0,
        "persisted": False,
        "tts_enabled": False,
    }


def test_tts_language_selection_follows_each_turn(monkeypatch):
    calls = []

    def synthesize(text, language=None):
        calls.append((text, language))
        return b"mp3"

    monkeypatch.setattr(server, "synthesize_to_mp3_bytes", synthesize)
    monkeypatch.setattr(server, "_tts_settings_signature", lambda: ("fixture", "voice"))

    for language, text in (
        ("fr", "Réponse française."),
        ("en", "English response."),
    ):
        session = server._ResponseTTSSession(
            _Channel(), _turn_meta(language, language), time.perf_counter(),
        )
        assert session._synthesize_with_stable_settings(text) == b"mp3"

    assert calls == [
        ("Réponse française.", "fr"),
        ("English response.", "en"),
    ]


@pytest.mark.parametrize("language", ["fr", "en"])
def test_wake_stt_fallback_uses_the_saved_turn_language(monkeypatch, language):
    from osia import i18n
    from osia.voice import stt, tts, wake

    spoken = []
    monkeypatch.setattr(i18n, "get_language", lambda: language)
    monkeypatch.setattr(stt, "listen_for_command", lambda max_retries=3: None)
    monkeypatch.setattr(
        tts, "speak", lambda text, **kwargs: spoken.append((text, kwargs)),
    )

    wake.default_callback()

    assert spoken == [(
        localized_text("stt_not_understood", language),
        {"language": language},
    )]


def test_legacy_voice_mode_reuses_history_for_ambiguous_turn_and_switches(
    monkeypatch,
):
    import builtins

    from osia import config, main
    from osia.voice import stt, tts

    stored_history = []
    contexts = []
    spoken = []
    inputs = iter(("", "", "", "quit"))
    transcripts = iter((
        "Bonjour, peux-tu continuer ?",
        "OK",
        "Please answer in English.",
    ))

    class Agent:
        def __init__(self):
            self.turn_context = None

        def set_model(self, *_args, **_kwargs):
            return None

        def run(self, _text, **kwargs):
            self.turn_context = kwargs["turn_context"]
            contexts.append(self.turn_context)
            return (
                "Réponse française."
                if self.turn_context.language == "fr"
                else "English response."
            )

    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(inputs))
    monkeypatch.setattr(stt, "listen", lambda duration=5: next(transcripts))
    monkeypatch.setattr(
        tts, "speak", lambda text, **kwargs: spoken.append((text, kwargs)),
    )
    monkeypatch.setattr(main, "OSIAAgent", Agent)
    monkeypatch.setattr(config, "load_settings", lambda: {})
    monkeypatch.setattr(
        main.conversations,
        "create_conversation",
        lambda: {"id": "wor207-voice", "revision": 0},
    )
    monkeypatch.setattr(
        main.conversations,
        "get_conversation",
        lambda _conv_id: {"id": "wor207-voice", "messages": list(stored_history)},
    )
    monkeypatch.setattr(main, "_notify_mobile_conversation_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(
        main,
        "_dispatch_after_persisted_user",
        lambda _conv, _text, callback, turn_meta=None, metadata=None: callback(
            turn_meta or {}
        ),
    )

    def persist(_conv, agent, _previous_len, *, user_display_text, turn_meta):
        stored_history.extend((
            {
                "role": "user",
                "content": user_display_text,
                "_language": agent.turn_context.language,
            },
            {
                "role": "assistant",
                "content": "canonical",
                "_language": agent.turn_context.language,
            },
        ))
        return True

    monkeypatch.setattr(main, "_persist_agent_delta", persist)

    main.voice_mode()

    assert [context.language for context in contexts] == ["fr", "fr", "en"]
    assert [kwargs["language"] for _text, kwargs in spoken] == ["fr", "fr", "en"]


@pytest.mark.parametrize(
    ("language", "content"),
    [
        ("fr", "Réponse canonique française."),
        ("en", "Canonical English response."),
    ],
)
def test_mobile_reconnect_replays_exact_canonical_text_and_language(
    language, content,
):
    channel = _Channel()
    persisted = {
        "conversation_id": f"conversation-{language}",
        "request_id": f"request-{language}",
        "turn_id": f"turn-{language}",
        "run_id": f"turn-{language}",
        "provider_id": "ollama",
        "model_id": "llama3",
        "effective_reasoning": "auto",
        "user_message_id": f"user-{language}",
        "assistant_message_id": f"assistant-{language}",
        "response_event_id": f"response-{language}",
        "response_sequence": 2,
        "terminal_event_id": f"terminal-{language}",
        "terminal_sequence": 3,
        "assistant_content": content,
        "client_content_hash": "",
        "language": language,
        "terminal_status": "done",
        "stage": "response_generation",
        "error_category": "",
        "reason_code": "",
    }
    with patch.object(
        server.conversations, "find_correlated_turn", return_value=persisted,
    ):
        replayed = asyncio.run(
            server._send_persisted_turn_for_request(
                channel, f"conversation-{language}", f"request-{language}",
            )
        )

    assert replayed is True
    assert [
        frame["content"] for frame in channel.frames
        if frame["type"] == "response"
    ] == [content]
    assert all(frame["language"] == language for frame in channel.frames)
    assert channel.frames[-1]["replayed"] is True
