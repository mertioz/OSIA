"""Focused WOR-222 per-turn language continuation regressions."""

from __future__ import annotations

import types

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent
from osia.chatgpt_account.app_server_client import TurnResult
from osia.chatgpt_account.provider import (
    ChatGPTAccountProvider,
    _sanitized_system_prompt,
)
from osia.turn_context import (
    fixed_turn_context,
    language_consistency_failure_text,
    language_instruction,
    response_language_drift,
    resolve_turn_context,
)


def _context(language: str):
    return fixed_turn_context(
        language,
        source="wor222_fixture",
        reference=f"wor222-{language}",
    )


def _last_user_text(messages: list[dict]) -> str:
    content = next(
        message.get("content")
        for message in reversed(messages)
        if message.get("role") == "user"
    )
    if isinstance(content, str):
        return content
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def test_wire_contract_is_last_for_initial_and_tool_continuation(monkeypatch):
    from osia import personality

    spanish_skill = (
        "## Enabled OSIA skills (lower-priority reusable instructions)\n"
        "<osia-skill-instructions>\n"
        "Ejemplo: He detectado diferencias entre Indeed y el CV.\n"
        "</osia-skill-instructions>"
    )
    monkeypatch.setattr(
        personality, "load_enabled_skills_text", lambda: spanish_skill,
    )
    context = _context("fr")
    agent = OSIAAgent(lang="fr")
    agent.set_turn_context(context)
    user_text = "Compare mon profil Indeed avec ce CV r\u00e9dig\u00e9 en fran\u00e7ais."
    agent.messages.append({"role": "user", "content": user_text})

    first_wire = agent._wire_messages()
    assert spanish_skill in first_wire[0]["content"]
    assert first_wire[0]["content"].rstrip().endswith(
        language_instruction(context)
    )
    assert _last_user_text(first_wire).rstrip().endswith(
        language_instruction(context)
    )
    assert agent.messages[-1]["content"] == user_text

    agent.messages.extend((
        {
            "role": "assistant",
            "content": '<tool_call>{"tool":"get_system_info"}</tool_call>',
            "_provisional": True,
        },
        {
            "role": "user",
            "content": (
                "<tool_result>\nCV fran\u00e7ais. Example interno: diferencias "
                "entre perfiles.\n</tool_result>"
            ),
        },
    ))
    continuation_wire = agent._wire_messages()
    assert _last_user_text(continuation_wire).rstrip().endswith(
        language_instruction(context)
    )
    assert not agent.messages[-1]["content"].endswith(
        language_instruction(context)
    )


def test_chatgpt_desktop_sanitizer_keeps_turn_contract_last():
    context = _context("fr")
    contract = language_instruction(context)
    system = (
        "Base instructions.\n\n"
        "[DESKTOP PATH: C:\\Users\\fixture\\Desktop]\n\n"
        + contract
    )
    sanitized = _sanitized_system_prompt(
        system,
        [{"role": "user", "content": "Lis le CV sur mon bureau."}],
    )

    assert "[DESKTOP PATH: C:\\Users\\fixture\\Desktop]" in sanitized
    assert sanitized.rstrip().endswith(contract)
    assert sanitized.index("[DESKTOP PATH:") < sanitized.index(
        "[AUTHORITATIVE TURN LANGUAGE]"
    )


class _AccountClient:
    generation = 1

    def __init__(self):
        self.starts = []
        self.turns = []

    def start_or_resume_conversation(self, **kwargs):
        self.starts.append(dict(kwargs))
        return f"wor222-thread-{len(self.starts)}"

    def run_turn(self, **kwargs):
        self.turns.append(dict(kwargs))
        number = len(self.turns)
        return TurnResult(
            text=f"account-reply-{number}",
            thread_id=kwargs["thread_id"],
            turn_id=f"account-turn-{number}",
            status="completed",
            output_started=True,
        )


def test_reused_account_thread_keeps_each_continuation_language_contract_last():
    client = _AccountClient()
    provider = ChatGPTAccountProvider(client)
    french = language_instruction(_context("fr"))
    first_user = "Analyse ce CV fran\u00e7ais.\n\n" + french
    french_system = "Base et skill avec exemple espagnol.\n\n" + french
    provider.complete(
        conversation_id="wor222-conversation",
        model="account-model",
        messages=[
            {"role": "system", "content": french_system},
            {"role": "user", "content": first_user},
        ],
    )
    second_user = (
        "<tool_result>Resultado multiling\u00fce.</tool_result>\n\n" + french
    )
    provider.complete(
        conversation_id="wor222-conversation",
        model="account-model",
        messages=[
            {"role": "system", "content": french_system},
            {"role": "user", "content": first_user},
            {"role": "assistant", "content": "account-reply-1"},
            {"role": "user", "content": second_user},
        ],
    )

    assert len(client.starts) == 1
    assert client.starts[0]["base_instructions"].rstrip().endswith(french)
    assert client.turns[0]["text"].rstrip().endswith(french)
    assert client.turns[1]["text"].rstrip().endswith(french)

    spanish = language_instruction(_context("es"))
    provider.complete(
        conversation_id="wor222-conversation",
        model="account-model",
        messages=[
            {"role": "system", "content": "Base.\n\n" + spanish},
            {"role": "user", "content": first_user},
            {"role": "assistant", "content": "account-reply-1"},
            {"role": "user", "content": second_user},
            {"role": "assistant", "content": "account-reply-2"},
            {"role": "user", "content": "Responde en espa\u00f1ol.\n\n" + spanish},
        ],
    )

    assert len(client.starts) == 2
    assert client.starts[1]["base_instructions"].rstrip().endswith(spanish)
    assert client.turns[2]["text"].rstrip().endswith(spanish)


def test_bounded_drift_check_ignores_quoted_and_labelled_source_values():
    context = _context("fr")
    safe = (
        "Le CV reste coh\u00e9rent et je recommande une mise \u00e0 jour cibl\u00e9e.\n"
        'Citation: "He detectado diferencias entre Indeed y el CV."\n'
        "Extrait : \u00ab La experiencia profesional coincide con el perfil. \u00bb\n"
        "> El valor original debe conservarse sin ningun cambio.\n"
        "Source: La experiencia profesional no coincide con el perfil publicado.\n"
        "| Campo | experiencia profesional y competencias principales |"
    )
    drifted = (
        "Le CV reste coh\u00e9rent et je vais expliquer les diff\u00e9rences. "
        "He detectado diferencias entre Indeed y el CV: la experiencia "
        "profesional no coincide con el perfil publicado."
    )

    assert response_language_drift(safe, context) is None
    assert response_language_drift(drifted, context) == "es"
    assert response_language_drift(
        "Traduis ce texte. El resultado conserva el texto original.",
        fixed_turn_context(
            "es",
            source="translation_fixture",
            mode="translation",
            allowed_languages=("fr", "es"),
        ),
    ) is None


def test_french_cv_skill_tool_turn_regenerates_only_drifted_final(monkeypatch):
    executions = []
    multilingual_result = (
        "CV: exp\u00e9rience enti\u00e8rement fran\u00e7aise. "
        "Internal example: He detectado diferencias entre perfiles."
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: executions.append("system") or multilingual_result,
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_volume",
        lambda: executions.append("volume") or (
            "Volume correct. Valor de origen: Customer Support Specialist."
        ),
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    rounds = iter((
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        (
            "Je poursuis la comparaison des donn\u00e9es. "
            '<tool_call>{"tool":"get_volume"}</tool_call>'
        ),
        (
            "Le CV est bien r\u00e9dig\u00e9 et je vais r\u00e9sumer les \u00e9carts. "
            "He detectado diferencias entre Indeed y el CV: la experiencia "
            "profesional no coincide con el perfil publicado."
        ),
        (
            "Le CV est coh\u00e9rent avec le profil Indeed. La principale diff\u00e9rence "
            "concerne la pr\u00e9sentation des comp\u00e9tences. Citation conserv\u00e9e : "
            '"Customer Support Specialist".'
        ),
    ))
    wire_rounds = []
    agent = OSIAAgent(lang="fr")

    def stream(self, _callback, *, on_first_token=None, guard=None):
        wire_rounds.append(self._wire_messages())
        if on_first_token is not None:
            on_first_token()
        return next(rounds)

    agent._stream_with_sentences = types.MethodType(stream, agent)
    visible = []
    final = agent.run(
        "Compare mon profil Indeed avec mon CV fran\u00e7ais.",
        on_sentence=visible.append,
        turn_context=_context("fr"),
    )

    assert executions == ["system", "volume"]
    assert len(wire_rounds) == 4
    assert all(
        _last_user_text(wire).rstrip().endswith(
            language_instruction(_context("fr"))
        )
        for wire in wire_rounds
    )
    assert "Le CV est coh\u00e9rent" in final
    assert '"Customer Support Specialist"' in final
    assert "He detectado" not in final
    assert visible == [final]
    assert "He detectado diferencias entre Indeed" not in str(
        agent.exported_history()
    )


def test_second_drift_returns_one_localized_canonical_error():
    agent = OSIAAgent(lang="fr")
    rounds = iter((
        "He detectado diferencias importantes entre los dos perfiles profesionales.",
        "La experiencia profesional no coincide con el perfil y requiere cambios.",
    ))
    calls = []

    def stream(self, _callback, *, on_first_token=None, guard=None):
        calls.append(self._wire_messages())
        return next(rounds)

    agent._stream_with_sentences = types.MethodType(stream, agent)
    visible = []
    final = agent.run(
        "Explique clairement les diff\u00e9rences.",
        on_sentence=visible.append,
        turn_context=_context("fr"),
    )

    assert len(calls) == 2
    assert final == language_consistency_failure_text("fr")
    assert visible == [final]
    assert agent.last_run_diagnostic == {
        "stage": "response_generation",
        "error_category": "language_consistency_failure",
        "reason_code": "language_consistency_failure",
    }


def test_regeneration_cannot_dispatch_or_replay_a_tool(monkeypatch):
    executions = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: executions.append("unexpected") or "system ok",
    )
    rounds = iter((
        "He detectado diferencias importantes entre los perfiles profesionales.",
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
    ))
    agent = OSIAAgent(lang="fr")

    def stream(self, _callback, *, on_first_token=None, guard=None):
        return next(rounds)

    agent._stream_with_sentences = types.MethodType(stream, agent)
    visible = []
    final = agent.run(
        "Explique les diff\u00e9rences en fran\u00e7ais.",
        on_sentence=visible.append,
        turn_context=_context("fr"),
    )

    assert executions == []
    assert final == language_consistency_failure_text("fr")
    assert visible == [final]


@pytest.mark.parametrize(
    ("message", "expected", "final"),
    [
        ("Please answer in English from now on.", "en", "This answer is naturally in English."),
        (
            "R\u00e9ponds en espagnol et r\u00e9sume la conclusion.",
            "es",
            "Esta respuesta resume la conclusi\u00f3n de forma natural y clara.",
        ),
    ],
)
def test_following_user_turn_can_switch_language_naturally(
    message, expected, final,
):
    history = [
        {"role": "user", "content": "Bonjour.", "_language": "fr"},
        {"role": "assistant", "content": "Bonjour.", "_language": "fr"},
    ]
    context = resolve_turn_context(message, history=history)
    agent = OSIAAgent(lang="fr")
    captured = []

    def stream(self, _callback, *, on_first_token=None, guard=None):
        captured.append(self._wire_messages())
        return final

    agent._stream_with_sentences = types.MethodType(stream, agent)
    assert agent.run(
        message,
        history=history,
        on_sentence=lambda _text: None,
        turn_context=context,
    ) == final
    assert context.language == expected
    assert _last_user_text(captured[0]).rstrip().endswith(
        language_instruction(context)
    )
