"""Prompt delivery, Thinking presentation and factual continuation fixtures."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from osia import agent as runtime, personality
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.execution_trace import validate_persisted_trace
from osia.gui.main_popup import _ChatThinkingTrace, _thinking_display_text
from osia.mcp_client import registry as mcp_registry
from osia.tool_contracts import ALL_TEXT_PROVIDERS
from osia.turn_context import localized_text
from test_wor340_thinking import ledger, payload
from tests.test_wor305_phase3a_no_replay import dispatch


def stable_prompt(provider):
    with (
        patch.object(personality, "load_personality", return_value=""),
        patch.object(personality, "load_enabled_skills_text", return_value=""),
        patch.object(mcp_registry, "mcp_tool_prompt_block", return_value=""),
    ):
        return runtime.get_system_prompt("fr", "deepseek-flash" if provider == "deepseek" else "fixture", provider)


@pytest.mark.parametrize("provider", ALL_TEXT_PROVIDERS)
def test_effective_wire_prompt_contains_one_proportionate_policy(provider):
    a = runtime.OSIAAgent(model="fixture", lang="fr")
    a.provider = provider
    a.messages = [{"role": "system", "content": stable_prompt(provider)},
                  {"role": "user", "content": "Ouvre l’application de test."}]
    wire = a._wire_messages()
    system = next(m["content"] for m in wire if m["role"] == "system")
    assert system.count("## Objective completion") == 1
    assert system.count("## Public progress updates") == 1
    assert system.count("## Available OSIA capabilities") == 1
    assert "natural first-person sentences in the user's language" in system
    assert "do not narrate every tool action" in system
    assert "Reuse fresh, authoritative receipts" in system
    assert "without a concrete reason" in system
    assert "Unknown effects remain uncertain" in system
    assert "Respect cancellation, target identity, and stale-state rejection" in system
    assert "never repeat a successful mutation" not in system
    assert "reconcile an uncertain effect before retrying" not in system
    assert "Retry only with positive evidence" not in system
    assert "French (fr)" in system


def test_actual_account_prepare_delivers_canonical_guidance_and_reuses_fresh_thread():
    captured = []
    class Client:
        generation = 1
        def start_or_resume_conversation(self, **kwargs):
            captured.append(kwargs)
            return "fixture-thread"
    provider = ChatGPTAccountProvider(client=Client())
    messages = [{"role": "system", "content": stable_prompt("chatgpt_account")},
                {"role": "user", "content": "Ouvre l’application de test."}]
    state, delta = provider._prepare("fixture-conv", "gpt-6-luna", messages)
    base = captured[0]["base_instructions"]
    assert "natural first-person sentences" in base
    assert "Reuse fresh, authoritative receipts" in base
    assert base.count("ChatGPT account capability context") == 1
    assert "[PROJECT ROOT:" not in base
    assert "French (fr)" in base.rsplit("[TURN RESPONSE LANGUAGE]", 1)[1]
    assert captured[0]["developer_instructions"] == (
        "Act as OSIA's provider. Use any applicable hosted or OSIA capability and report only observed outcomes.")
    assert "open_application" in {t["name"] for t in captured[0]["dynamic_tools"]}
    provider._prepare("fixture-conv", "gpt-6-luna", messages)
    assert len(captured) == 1  # No extra thread/request for formatting.


@pytest.mark.parametrize(("raw", "visible"), [
    ("**Opening Steam and navigating website**", "Opening Steam and navigating website"),
    ("**Je vais ouvrir l’application.**", "Je vais ouvrir l’application."),
    ("Before\n  **Heading**  \nAfter", "Before\n  Heading  \nAfter"),
    ("Inline **bold** text", "Inline **bold** text"),
    ("**[site](https://fixture.invalid)**", "**[site](https://fixture.invalid)**"),
    ("**`code`**", "**`code`**"),
    ("**name_with_markup**", "**name_with_markup**"),
    ("**<b>HTML</b>**", "**<b>HTML</b>**"),
    ("    **indented code**", "    **indented code**"),
    ("\t**indented code**", "\t**indented code**"),
    ("```text\n**code**\n```\n**Heading**", "```text\n**code**\n```\nHeading"),
    ("~~~~\n**code**\n~~~\n**still code**\n~~~~", "~~~~\n**code**\n~~~\n**still code**\n~~~~"),
    ("**unfinished", "**unfinished"),
])
def test_thinking_only_simple_markdown_presentation(raw, visible):
    assert _thinking_display_text(raw) == visible


_APPS = []
def test_widget_renders_heading_and_preserves_raw_trace_and_live_revision():
    app = QApplication.instance() or QApplication([])
    _APPS.append(app)
    t = ledger()
    raw = "**Opening Steam and navigating website**"
    t.record_public_reasoning_summary(raw, source_id="provider-summary")
    before = json.dumps(t.snapshot(), sort_keys=True)
    widget = _ChatThinkingTrace()
    try:
        widget.apply_trace(payload(t))
        assert widget._detail.toPlainText() == raw[2:-2]
        assert json.dumps(t.snapshot(), sort_keys=True) == before
        assert widget._source_payload["snapshot"][0]["summary"] == raw
        t.record_public_reasoning_summary(raw + "\nJe poursuis.", source_id="provider-summary")
        widget.apply_trace(payload(t))
        assert widget._detail.toPlainText() == raw[2:-2] + "\nJe poursuis."
        assert validate_persisted_trace(t.snapshot())
        assert widget._expanded
    finally:
        widget.close()


@pytest.mark.parametrize("provider", ["chatgpt_account", "deepseek", "openai"])
@pytest.mark.parametrize("scenario", ["adequate", "uncertain", "error", "multi"])
def test_canonical_factual_results_determine_next_model_choice(dispatch, monkeypatch, provider, scenario):
    """Scripted decisions test feedback/dispatch, not live model obedience."""
    a = runtime.OSIAAgent(model="fixture", lang="en")
    a.provider = provider
    a.model = "deepseek-flash" if provider == "deepseek" else "fixture"
    a._learning_engine = SimpleNamespace(ingest_terminal_attempts=lambda: None, context_for_request=lambda _: "")
    invocations, rounds, finals = [], [], []
    first = {"ok": True, "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed", "outcome": "confirmed_success",
             "evidence": [{"window_ready": True, "focused": True}]}}
    if scenario == "uncertain":
        first = {"ok": False, "receipt": {"schema_version": 1, "dispatch_state": "dispatched_outcome_unknown", "outcome": "outcome_unknown"}}
    elif scenario == "error":
        first = {"ok": False, "receipt": {"schema_version": 1, "dispatch_state": "not_dispatched", "outcome": "failed_before_effect"}}
    def call(name, result):
        def invoke(**_kwargs):
            invocations.append(name)
            return result
        return invoke
    monkeypatch.setitem(runtime.TOOL_MAP, "open_application", call("open_application", first))
    monkeypatch.setitem(runtime.TOOL_MAP, "has_window", call("has_window", True))
    monkeypatch.setitem(runtime.TOOL_MAP, "navigate_browser", call("navigate_browser", {
        "ok": True, "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed", "outcome": "confirmed_success",
                                  "evidence": [{"navigation_commit": True}]}}))
    step = 0
    final = "The fixture window is ready." if scenario != "multi" else "The fixture window and site are ready."
    def create(**kwargs):
        nonlocal step
        rounds.append(kwargs)
        content = json.dumps(kwargs["messages"], ensure_ascii=False)
        if step == 0:
            output = '<tool_call>{"tool":"open_application","name":"FixtureApp"}</tool_call>'
        elif step == 1:
            assert ("confirmed_success" if scenario in ["adequate", "multi"] else
                    "outcome_unknown" if scenario == "uncertain" else "failed_before_effect") in content
            assert "Use these factual results and fresh existing evidence" in content
            assert "Reassess the full objective" not in content
            output = {
                "adequate": final,
                "uncertain": '<tool_call>{"tool":"has_window","app":"FixtureApp"}</tool_call>',
                "error": '<tool_call>{"tool":"open_application","name":"FixtureApp"}</tool_call>',
                "multi": '<tool_call>{"tool":"navigate_browser","url":"https://fixture.invalid"}</tool_call>',
            }[scenario]
            if scenario == "error":
                monkeypatch.setitem(runtime.TOOL_MAP, "open_application", call("open_application", {
                    "ok": True, "receipt": {"schema_version": 1, "dispatch_state": "effect_confirmed", "outcome": "confirmed_success"}}))
        elif step == 2 and scenario != "adequate":
            assert ("true" if scenario == "uncertain" else "confirmed_success") in content
            output = final
        else:
            pytest.fail("Unexpected extra fixture model round")
        step += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output, tool_calls=None))])
    a.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    a.configure_tool_attempts(conversation_id="fixture", turn_id=f"fixture-{provider}-{scenario}", request_id="fixture", final_response_message_id="fixture-final")
    # Test the shared Chat tool loop directly; driver coverage remains in the
    # WOR-341 canonical-driver regression. Explicit non-stream fixture output.
    def stream(callback, **kwargs):
        return create(messages=a._wire_messages(), model=a.model).choices[0].message.content
    a._stream_with_sentences = stream
    result = a.run("Open the fixture application" + (" and site." if scenario == "multi" else "."),
                   history=[], on_sentence=finals.append)
    assert result == final and finals == [final]
    assert invocations == ["open_application"] + ([] if scenario == "adequate" else
        ["has_window"] if scenario == "uncertain" else ["open_application"] if scenario == "error" else ["navigate_browser"])
    assert len(rounds) == (2 if scenario == "adequate" else 3)
    assert all(r["model"] == a.model for r in rounds)


@pytest.mark.parametrize("lang", ["en", "fr", "zh", "ja", "de", "es"])
def test_result_reinjection_keeps_localized_factual_guidance(lang):
    text = localized_text("tools_complete_instruction", lang)
    assert text and "<tool_call>" not in text
