"""The explicit /web request asks for research without vetoing a useful answer."""

from __future__ import annotations

import pytest

from osia import agent as runtime, conversations
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.slash_commands import (
    CapabilityRequirements, SlashTurnDirectivesV1, apply_to_agent,
    chat_command_scope, validate_live,
)
from osia.tools import public_web


_WEB = SlashTurnDirectivesV1(
    capability_requirements=CapabilityRequirements(current_web=True),
)


def test_web_instruction_reaches_the_selected_model_only_for_its_turn():
    ordinary = runtime.get_system_prompt(model="deepseek-flash", provider="deepseek")
    with chat_command_scope("web-prompt", _WEB):
        selected = runtime.get_system_prompt(model="deepseek-flash", provider="deepseek")
    following = runtime.get_system_prompt(model="deepseek-flash", provider="deepseek")
    assert "[User selected /web for this turn.]" in selected
    assert "even for a simple question" in selected
    assert "before answering" in selected
    assert "existing knowledge" in selected
    assert "[User selected /web for this turn.]" not in ordinary + following


@pytest.mark.parametrize("provider,model", [
    ("deepseek", "deepseek-flash"), ("openrouter", "fixture/model"),
])
@pytest.mark.parametrize("search_failure", ["empty", "offline"])
def test_attempted_search_may_fail_and_still_return_the_normal_answer(
    monkeypatch, provider, model, search_failure,
):
    from test_wor227_invalid_arguments import _install_rounds

    requests = []
    records = []
    execute = runtime._execute_tool_with_policy

    def record_attempt(*args, **kwargs):
        record = execute(*args, **kwargs)
        records.append(record)
        return record

    def request(url, *_args):
        requests.append(url)
        if search_failure == "offline":
            raise OSError("fixture network unavailable")
        if "bing.com" in url:
            return public_web._Response(
                "application/rss+xml", body=b"<rss><channel /></rss>",
            )
        return public_web._Response("text/html", body=b"<html></html>")

    monkeypatch.setattr(public_web, "_request_once", request)
    monkeypatch.setattr(runtime, "_execute_tool_with_policy", record_attempt)
    monkeypatch.setattr(runtime, "_get_browser_tools", lambda: pytest.fail("Unexpected browser launch"))
    selected = runtime.OSIAAgent(model="fixture", lang="fr")
    selected.provider, selected.model = provider, model
    apply_to_agent(selected, _WEB)
    observations = []
    answer = "Je ne peux pas confirmer une source web ici. Un lapin est un mammifère lagomorphe."
    _install_rounds(selected, [
        '<tool_call>{"tool":"search_public_web","query":"qu’est-ce qu’un lapin"}</tool_call>',
        answer, answer,
    ], observations)

    assert selected.run("C’est quoi un lapin ?", history=[], on_sentence=lambda _: None) == answer
    assert "[User selected /web for this turn.]" in observations[0]["messages"]
    assert len(requests) == 2  # Both existing public sources were actually attempted.
    assert selected.last_slash_capability_receipt is None  # Failure is not fabricated success.
    assert {(item["provider"], item["model"]) for item in observations} == {(provider, model)}
    assert len(records) == 1 and records[0].dispatched and records[0].is_error
    assert records[0].result["error"]["code"].startswith("public_search_")


def test_web_request_stays_sendable_when_no_web_capability_is_available(monkeypatch):
    monkeypatch.setattr("osia.slash_commands.connector_availability", lambda: ())
    monkeypatch.setattr("osia.slash_commands.current_web_available", lambda *_: False)
    assert validate_live(_WEB) is _WEB


def test_account_reused_thread_receives_the_current_web_instruction(tmp_path, monkeypatch):
    from test_codex_context_capacity import _Runtime

    monkeypatch.setattr(conversations, "CONVERSATIONS_DIR", tmp_path / "conversations")
    conversation_id = conversations.create_conversation()["id"]
    client = _Runtime()
    provider = ChatGPTAccountProvider(client)
    normal = runtime.get_system_prompt(model="gpt-6.1-sol", provider="chatgpt_account")
    provider.complete(
        conversation_id=conversation_id, model="gpt-6.1-sol", osia_turn_id="first",
        messages=[{"role": "system", "content": normal}, {"role": "user", "content": "Bonjour"}],
    )
    with chat_command_scope(conversation_id, _WEB):
        research = runtime.get_system_prompt(model="gpt-6.1-sol", provider="chatgpt_account")
    provider.complete(
        conversation_id=conversation_id, model="gpt-6.1-sol", osia_turn_id="second",
        messages=[{"role": "system", "content": research},
                  {"role": "user", "content": "Bonjour"}, {"role": "assistant", "content": "answer"},
                  {"role": "user", "content": "C’est quoi un lapin ?"}],
    )
    assert len(client.starts) == 1
    assert "[User selected /web for this turn.]" in client.turns[-1]["application_context"]
    assert client.turns[-1]["thread_id"] == client.turns[0]["thread_id"]
