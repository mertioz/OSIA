import asyncio
import types
from unittest.mock import patch

from osia import agent as agent_module
from osia.agent import OSIAAgent, _sanitize_persisted_history_message
from osia.server import server
from osia.turn_context import localized_text


def _install_stream_rounds(agent, rounds):
    remaining = iter(rounds)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        content, provisional = next(remaining)
        if on_first_token is not None:
            on_first_token()
        for fragment in provisional:
            callback(fragment)
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_tool_round_prose_is_discarded_and_only_final_is_exported(monkeypatch):
    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: calls.append("system") or "system ok",
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_volume",
        lambda: calls.append("volume") or "volume ok",
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False
    )
    agent = OSIAAgent(lang="fr")
    _install_stream_rounds(agent, [
        (
            "Je vais vérifier.\n"
            '<tool_call>{"tool":"get_system_info"}</tool_call>\n'
            '<tool_call>{"tool":"get_volume"}</tool_call>',
            ["Je vais vérifier."],
        ),
        ("Vérification terminée.", ["Vérification terminée."]),
    ])
    visible = []

    final = agent.run(
        "Peux-tu vérifier le système et le volume ?",
        on_sentence=visible.append,
    )

    assert calls == ["system", "volume"]
    assert final == "Vérification terminée."
    assert visible == [final]
    exported = agent.exported_history()
    assert [item["role"] for item in exported] == ["user", "assistant"]
    assert exported[-1]["content"] == final
    assert all("Je vais vérifier" not in str(item) for item in exported)
    assert all("tool_call" not in str(item) for item in exported)

    # Reusing this in-process agent must not replay the prior provisional/tool
    # scaffolding to the provider on the next user turn.
    assert any(item.get("_provisional") for item in agent.messages)
    _install_stream_rounds(agent, [("Nouveau final.", ["Nouveau final."])])
    second_visible = []
    second = agent.run("Merci, continue.", on_sentence=second_visible.append)
    assert second_visible == [second]
    assert all(not item.get("_provisional") for item in agent.messages)
    assert all("tool_result" not in str(item) for item in agent.messages)


def test_empty_provider_rounds_yield_one_localized_non_success_fallback():
    agent = OSIAAgent(lang="fr")
    _install_stream_rounds(agent, [
        ("<think>brouillon privé</think>", ["brouillon privé"]),
        ("<think>autre brouillon</think>", ["autre brouillon"]),
    ])
    visible = []

    final = agent.run(
        "Réponds en français à cette demande.",
        on_sentence=visible.append,
    )

    assert final == localized_text("empty_completion", "fr")
    assert visible == [final]
    assert "exécuté" not in final.casefold()
    assert all("brouillon" not in str(item) for item in agent.exported_history())


def test_provisional_history_frames_are_never_persistable():
    assert _sanitize_persisted_history_message({
        "role": "assistant",
        "content": "draft",
        "_provisional": True,
    }) is None


class _Channel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


class _ServerAgent:
    model = "test-model"
    provider = "test-provider"
    _applied_selected_model = None
    _in_use = False

    def __init__(self, final="Résultat canonique.", error=None):
        self.final = final
        self.error = error

    def run(self, _text, *, on_sentence, on_first_token, **_kwargs):
        on_first_token()
        on_sentence("Brouillon avant outil.")
        on_sentence("Deuxième brouillon.")
        if self.error is not None:
            raise self.error
        on_sentence(self.final)
        return self.final

    def exported_history(self):
        return [
            {"role": "user", "content": "demande"},
            {"role": "assistant", "content": self.final},
        ]


def _meta(suffix):
    return {
        "conversation_id": f"wor177-conv-{suffix}",
        "request_id": f"wor177-request-{suffix}",
        "client_request_id": f"wor177-request-{suffix}",
        "turn_id": f"wor177-turn-{suffix}",
        "user_message_id": f"wor177-user-{suffix}",
        "assistant_message_id": f"wor177-assistant-{suffix}",
        "_event_sequence": 0,
        "persisted": False,
        "tts_enabled": False,
        "client_locale": "fr-FR",
    }


async def _run_server_turn(agent, suffix):
    channel = _Channel()
    persisted = []
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
        await server._run_agent_and_respond(
            channel,
            "Peux-tu exécuter cette demande ?",
            [],
            f"wor177-conv-{suffix}",
            get_agent=lambda _conv: agent,
            turn_meta=_meta(suffix),
        )
    return channel.frames, persisted


def test_server_sends_persists_and_replays_only_the_canonical_final():
    frames, persisted = asyncio.run(_run_server_turn(_ServerAgent(), "success"))
    chunks = [frame["content"] for frame in frames if frame["type"] == "response_chunk"]
    finals = [frame["content"] for frame in frames if frame["type"] == "response"]
    assert chunks == ["Résultat canonique."]
    assert finals == chunks
    assert persisted == finals
    assert not any("Brouillon" in str(frame) for frame in frames)
    assert all(frame.get("language") == "fr" for frame in frames if "turn_id" in frame)


def test_server_failure_discards_drafts_and_hides_exception_payload(capsys):
    secret = "PRIVATE_PROVIDER_PAYLOAD"
    frames, persisted = asyncio.run(
        _run_server_turn(_ServerAgent(error=RuntimeError(secret)), "failure")
    )
    fallback = localized_text("provider_failure", "fr")
    chunks = [frame["content"] for frame in frames if frame["type"] == "response_chunk"]
    finals = [frame["content"] for frame in frames if frame["type"] == "response"]
    assert chunks == [fallback]
    assert finals == [fallback]
    assert persisted == [fallback]
    assert secret not in str(frames)
    assert secret not in capsys.readouterr().out
    terminal = [frame for frame in frames if frame.get("terminal") is True][-1]
    assert terminal["error_category"] == "provider_failure"
    assert terminal["reason_code"] == "provider_failure"


def test_server_empty_final_never_promotes_drafts_and_is_typed():
    frames, persisted = asyncio.run(_run_server_turn(_ServerAgent(final=""), "empty"))
    fallback = localized_text("empty_completion", "fr")
    chunks = [frame["content"] for frame in frames if frame["type"] == "response_chunk"]
    assert chunks == [fallback]
    assert persisted == [fallback]
    assert not any("Brouillon" in str(frame) for frame in frames)
    terminal = [frame for frame in frames if frame.get("terminal") is True][-1]
    assert terminal["error_category"] == "empty_completion"
    assert terminal["reason_code"] == "empty_completion"


def test_reconnect_replays_exact_persisted_final_and_failure_type():
    channel = _Channel()
    final = localized_text("empty_completion", "fr")
    persisted = {
        "turn_id": "turn-replay",
        "user_message_id": "user-replay",
        "assistant_message_id": "assistant-replay",
        "response_event_id": "event-response-replay",
        "response_sequence": 2,
        "terminal_event_id": "event-terminal-replay",
        "terminal_sequence": 3,
        "assistant_content": final,
        "client_content_hash": "",
        "language": "fr",
        "terminal_status": "done",
        "stage": "response_generation",
        "error_category": "empty_completion",
        "reason_code": "empty_completion",
    }
    with patch.object(server.conversations, "find_correlated_turn", return_value=persisted):
        replayed = asyncio.run(
            server._send_persisted_turn_for_request(
                channel, "conv-replay", "request-replay"
            )
        )
    assert replayed is True
    assert [frame["content"] for frame in channel.frames if frame["type"] == "response"] == [final]
    assert all(frame["language"] == "fr" for frame in channel.frames)
    terminal = channel.frames[-1]
    assert terminal["error_category"] == "empty_completion"
    assert terminal["replayed"] is True
