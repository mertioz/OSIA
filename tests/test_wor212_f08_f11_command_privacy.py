"""Deterministic WOR-212 F08 command grammar and F11 privacy regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from osia.voice import commands, stt, tts


FRENCH_COMMANDS = (
    "nouveau chat",
    "nouvelle conversation",
    "nouvelle discussion",
    "démarre un nouveau chat",
)
ENGLISH_COMMANDS = (
    "new chat",
    "new conversation",
    "start a new chat",
)
OTHER_EXISTING_COMMANDS = (
    "新对话",
    "新對話",
    "新聊天",
    "新的对话",
    "新しいチャット",
    "新しい会話",
    "neues gespräch",
    "neue unterhaltung",
    "neuer chat",
    "nueva conversación",
    "nueva conversacion",
    "nuevo chat",
)
NEGATIVE_MENTIONS = (
    "le nouveau chat semble utile",
    "parle-moi du nouveau chat",
    "ce n’est pas un nouveau chat",
    "est-ce un nouveau chat ?",
    "the new chat seems useful",
    "tell me about the new chat",
    "this is not a new chat",
    "is this a new chat?",
    "démarre un nouveau chat maintenant",
    "please start a new chat",
    "explique-moi comment organiser cette discussion",
)
PRIVACY_CANARY = "WOR212PrivacyCanary violet orbit seven"
CANARY_PARTS = (
    "WOR212PrivacyCanary",
    "PrivacyCanary",
    "violet",
    "orbit seven",
)


def _route_accepted_voice_text(text: str, calls: dict[str, list[str]]) -> None:
    """Mirror the local-command boundary without invoking GUI/provider code."""
    if commands.is_new_chat_command(text):
        calls["local"].append("new_conversation")
        return
    calls["agent"].append(text)


def _result(
    text: str,
    *,
    accepted: bool,
    reasons: tuple[str, ...] = (),
) -> stt.TranscriptionResult:
    return stt.TranscriptionResult(
        text=text,
        language="fr",
        language_probability=0.99,
        segment_count=1,
        average_log_probability=-0.2,
        minimum_average_log_probability=-0.2,
        maximum_no_speech_probability=0.02,
        maximum_compression_ratio=1.1,
        classification="accepted" if accepted else "rejected",
        rejection_reasons=reasons,
    )


def _retained_output(capsys, caplog) -> str:
    captured = capsys.readouterr()
    logs = "\n".join(record.getMessage() for record in caplog.records)
    return "\n".join((captured.out, captured.err, logs))


def _assert_private(output: str) -> None:
    for part in CANARY_PARTS:
        assert part not in output


@pytest.mark.parametrize("phrase", FRENCH_COMMANDS + ENGLISH_COMMANDS)
def test_every_approved_french_and_english_command_is_anchored(phrase):
    calls = {"local": [], "agent": []}

    _route_accepted_voice_text(phrase, calls)

    assert calls == {"local": ["new_conversation"], "agent": []}


@pytest.mark.parametrize("phrase", OTHER_EXISTING_COMMANDS)
def test_existing_non_french_english_exact_commands_remain_supported(phrase):
    assert commands.is_new_chat_command(phrase)


@pytest.mark.parametrize(
    "phrase",
    (
        "  NOUVEAU   CHAT  ",
        "Nouvelle Conversation.",
        "DÉMARRE   UN NOUVEAU CHAT !",
        "  NEW   CHAT?  ",
        "Start A New Chat…",
        "新对话。",
    ),
)
def test_safe_case_whitespace_accent_and_terminal_punctuation_variants(phrase):
    assert commands.is_new_chat_command(phrase)


@pytest.mark.parametrize("phrase", NEGATIVE_MENTIONS)
def test_mentions_negations_questions_and_longer_sentences_use_agent_path(phrase):
    calls = {"local": [], "agent": []}

    _route_accepted_voice_text(phrase, calls)

    assert calls == {"local": [], "agent": [phrase]}


@pytest.mark.parametrize(
    "phrase",
    ("new-chat", "nouveau/chat", "start-a-new-chat", "nouveau chat; ensuite"),
)
def test_internal_punctuation_is_not_erased_into_a_command(phrase):
    assert not commands.is_new_chat_command(phrase)


def test_f01_rejection_stops_before_command_matching(monkeypatch, capsys, caplog):
    matcher_calls = []
    monkeypatch.setattr(
        stt,
        "record_audio_vad",
        lambda **_kwargs: np.ones(32, dtype=np.float32),
    )
    monkeypatch.setattr(
        stt,
        "transcribe",
        lambda *_args, **_kwargs: _result(
            "nouveau chat",
            accepted=False,
            reasons=("low_average_log_probability",),
        ),
    )
    monkeypatch.setattr(
        commands,
        "is_new_chat_command",
        lambda text: matcher_calls.append(text) or True,
    )

    text = stt.listen_for_command(max_retries=1)
    calls = {"local": [], "agent": []}
    if text:
        _route_accepted_voice_text(text, calls)

    assert text == ""
    assert matcher_calls == []
    assert calls == {"local": [], "agent": []}
    assert "low_average_log_probability" in _retained_output(capsys, caplog)


def test_accepted_rejected_and_retry_diagnostics_keep_only_metadata(
    monkeypatch,
    capsys,
    caplog,
):
    results = iter(
        (
            _result(
                PRIVACY_CANARY,
                accepted=False,
                reasons=("low_average_log_probability",),
            ),
            _result(PRIVACY_CANARY, accepted=True),
        )
    )
    monkeypatch.setattr(
        stt,
        "record_audio_vad",
        lambda **_kwargs: np.ones(32, dtype=np.float32),
    )
    monkeypatch.setattr(stt, "transcribe", lambda *_args, **_kwargs: next(results))

    assert stt.listen_for_command(max_retries=2) == PRIVACY_CANARY
    output = _retained_output(capsys, caplog)

    _assert_private(output)
    assert "attempt 1/2" in output
    assert "attempt 2/2" in output
    assert "Transcript rejected (low_average_log_probability)" in output
    assert "Transcript accepted (language=fr, segments=1)" in output


def test_no_speech_and_local_command_paths_do_not_print_voice_content(
    monkeypatch,
    capsys,
    caplog,
):
    monkeypatch.setattr(
        stt,
        "record_audio_vad",
        lambda **_kwargs: np.zeros(0, dtype=np.float32),
    )
    assert stt.listen_for_command(max_retries=1) == ""
    assert commands.is_new_chat_command("NOUVEAU CHAT")
    output = _retained_output(capsys, caplog)

    assert "Silent chunk, retrying" in output
    assert "Max retries reached" in output
    assert "nouveau chat" not in output.lower()

    main_source = (Path(__file__).parents[1] / "osia" / "main.py").read_text(
        encoding="utf-8"
    )
    voice_path = main_source[
        main_source.index("def handle_wake("):
        main_source.index("# ── Text handler")
    ]
    assert "Voice command recognised: new chat" not in voice_path
    assert "local command accepted" in voice_path
    assert "action=new_conversation" in voice_path
    assert "bridge.chat_message_for_conversation.emit(conv_id, \"user\", user_text)" in voice_path
    assert "_run_agent(user_text" in voice_path


def test_direct_tts_diagnostics_report_length_without_spoken_text(
    monkeypatch,
    capsys,
    caplog,
):
    monkeypatch.setattr(tts, "_AUDIO_OK", False)
    session = tts.start_tts_session()

    tts.speak(PRIVACY_CANARY, session_id=session)
    output = _retained_output(capsys, caplog)

    _assert_private(output)
    assert f"Speaking ({len(PRIVACY_CANARY)} chars)" in output
    assert "no audio backend" in output


def test_tts_exception_diagnostic_cannot_append_spoken_text(
    monkeypatch,
    capsys,
    caplog,
):
    fake_sd = SimpleNamespace(stop=lambda: None)
    monkeypatch.setattr(tts, "sd", fake_sd)
    monkeypatch.setattr(tts, "_AUDIO_OK", True)
    monkeypatch.setattr(tts, "get_active_tts_provider", lambda: "local")
    monkeypatch.setattr(
        tts,
        "_get_kokoro_pipeline",
        lambda _lang: (_ for _ in ()).throw(RuntimeError(PRIVACY_CANARY)),
    )
    session = tts.start_tts_session()

    tts.speak(PRIVACY_CANARY, session_id=session)
    output = _retained_output(capsys, caplog)

    _assert_private(output)
    assert "Kokoro synthesis failed (RuntimeError)" in output
    assert f"Speaking ({len(PRIVACY_CANARY)} chars)" in output


def test_manual_voice_response_diagnostics_do_not_print_response_content():
    root = Path(__file__).parents[1]
    main_source = (root / "osia" / "main.py").read_text(encoding="utf-8")
    wake_source = (root / "osia" / "voice" / "wake.py").read_text(
        encoding="utf-8"
    )

    assert 'print(f"💬 OSIA: {response}")' not in main_source
    assert 'print(f"💬 OSIA: {response}")' not in wake_source
    assert "assistant response accepted" in main_source
    assert "assistant response accepted" in wake_source
    assert "_persist_exchange(" in main_source
    assert "bridge.assistant_stream_final.emit(conv_id, display)" in main_source
