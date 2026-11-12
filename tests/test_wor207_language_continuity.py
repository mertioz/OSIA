"""UI localization, speech and Mobile replay with model-owned response language."""

import asyncio
import time
from unittest.mock import patch

import pytest

from osia.server import server
from osia.turn_context import localized_text


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
        "language_mode": "model",
        "_event_sequence": 0,
        "persisted": False,
        "tts_enabled": False,
    }


def test_tts_uses_emitted_text_without_a_model_language_target(monkeypatch):
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
        ("Réponse française.", None),
        ("English response.", None),
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
    outputs = iter(("Réponse française.", "Réponse française.", "English response."))
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
            return next(outputs)

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
                "_language_mode": "model",
            },
            {
                "role": "assistant",
                "content": "canonical",
                "_language_mode": "model",
            },
        ))
        return True

    monkeypatch.setattr(main, "_persist_agent_delta", persist)

    main.voice_mode()

    assert [context.mode for context in contexts] == ["model"] * 3
    assert all(context.target_language is None for context in contexts)
    assert [kwargs["language"] for _text, kwargs in spoken] == [None] * 3
    assert [text for text, _kwargs in spoken] == [
        "Réponse française.", "Réponse française.", "English response.",
    ]


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
