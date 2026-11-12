"""Documented provider structures; deterministic fixtures, never live evidence."""
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from openai import OpenAI

from osia.agent import (
    OSIAAgent, _extract_tool_calls, _extract_public_progress_narration,
    _live_public_progress_narration, strip_reasoning_blocks,
)
from osia.chatgpt_account.app_server_client import AppServerClient, _TurnAttempt
from osia.execution_trace import ExecutionTraceLedger
from osia.public_commentary import PublicCommentaryStream, public_assistant_text


def delta(content=None, calls=None, **fields):
    return NS(content=content, tool_calls=calls, **fields)


def native(index=0, name="list_files", arguments='{"path":"fixture"}', identity="call1"):
    return NS(index=index, id=identity, type="function",
              function=NS(name=name, arguments=arguments))


def projector(provider="deepseek", *, round_id="round1", trace=None, timeline=None):
    trace = trace or ExecutionTraceLedger(
        conversation_id="conv_v3", request_id="req_v3", turn_id="turn_v3",
        provider_id=provider, model_id="deepseek-flash" if provider == "deepseek" else "fixture",
    )
    def emit(text, source):
        trace.record_public_reasoning_summary(text, source_id=source)
        if timeline is not None:
            timeline.append(("commentary", text))
    return PublicCommentaryStream(
        provider=provider, model=trace.model_id, turn_id=trace.turn_id, round_id=round_id,
        parse=lambda text, message: _extract_tool_calls(text, message, provider),
        clean=lambda text: _extract_public_progress_narration(strip_reasoning_blocks(text)),
        clean_live=_live_public_progress_narration, emit=emit,
    ), trace


@pytest.mark.parametrize("provider", ["deepseek", "openai", "google", "groq", "xai", "mistral", "ollama", "anthropic"])
def test_native_public_text_is_live_only_once_tool_context_is_known(provider):
    events = []
    stream, trace = projector(provider, timeline=events)
    stream.observe(delta("I will inspect the folder."))
    assert not events  # Ordinary assistant text is ambiguous before a call.
    stream.observe(delta(calls=[native(arguments='{"path":')]))
    assert events == [("commentary", "I will inspect the folder.")]
    stream.observe(delta(" I will compare its entries.", calls=[native(name=None, identity=None, arguments='"fixture"}')]))
    assert events[-1][1] == "I will inspect the folder. I will compare its entries."
    assert _extract_tool_calls(stream.text, stream.message, provider) == [{"tool": "list_files", "path": "fixture"}]
    stream.finish()
    assert len(trace.snapshot()["events"]) == 1
    assert trace.snapshot()["events"][0]["revision"] == 2


def test_deepseek_private_reasoning_and_silent_tools_do_not_create_commentary():
    stream, trace = projector()
    stream.observe(delta(reasoning_content="private chain of thought"))
    stream.observe(delta(calls=[native()]))
    stream.finish()
    assert trace.snapshot()["events"] == []
    assert _extract_tool_calls(stream.text, stream.message, "deepseek")


@pytest.mark.parametrize("provider", ["deepseek", "openai", "mistral", "groq"])
def test_final_only_or_after_tool_retrospective_stays_final(provider):
    stream, trace = projector(provider)
    if provider == "mistral":
        stream.observe(delta([{"type": "thinking", "thinking": [{"type": "text", "text": "private"}]}]))
        stream.observe(delta([{"type": "text", "text": "I inspected the folder; it contains two files."}]))
    else:
        stream.observe(delta("I inspected the folder; "))
        stream.observe(delta("it contains two files."))
    stream.finish()
    assert stream.text == "I inspected the folder; it contains two files."
    assert trace.snapshot()["events"] == []


def test_textual_call_classification_waits_for_valid_syntax_then_emits_before_end():
    stream, trace = projector("groq")
    stream.observe(delta("I will inspect the folder. <tool_"))
    assert not trace.snapshot()["events"]
    stream.observe(delta('call>{"tool":"list_files","path":"fixture"}</tool_call>'))
    assert trace.snapshot()["events"][0]["summary"] == "I will inspect the folder."
    stream.observe(delta(" I will then compare the entries."))
    assert trace.snapshot()["events"][0]["summary"] == "I will inspect the folder.  I will then compare the entries."
    # Completed protocol is stripped; genuine following prose is also live.
    stream.finish()
    assert trace.snapshot()["events"][0]["summary"] == "I will inspect the folder.  I will then compare the entries."


def test_multiround_timeline_preserves_tool_execution_and_final_order(caplog):
    caplog.set_level("DEBUG", logger="osia.public_commentary")
    timeline = []
    first, trace = projector(timeline=timeline)
    first.observe(delta("I will inspect the folder.", calls=[native()]))
    first.finish()
    timeline.append(("tool", "first receipt"))
    second, _ = projector(round_id="round2", trace=trace, timeline=timeline)
    second.observe(delta("The listing arrived. I will inspect the second folder.", calls=[native()]))
    second.finish()
    timeline.append(("tool", "second receipt"))
    last, _ = projector(round_id="round3", trace=trace, timeline=timeline)
    last.observe(delta("I inspected both folders. Here is the result."))
    last.finish()
    timeline.append(("final", last.text))
    trace.record(kind="terminal", display_key="execution.terminal.success", terminal=True)
    assert [kind for kind, _ in timeline] == ["commentary", "tool", "commentary", "tool", "final"]
    assert [e["summary"] for e in trace.snapshot()["events"] if "summary" in e] == [timeline[0][1], timeline[2][1]]
    assert "boundary=commentary_emitted" in caplog.text
    assert "boundary=response_complete" in caplog.text
    assert "I will inspect" not in caplog.text


def test_buffered_provider_content_is_emitted_at_its_actual_receipt_boundary():
    stream, trace = projector()
    assert not trace.snapshot()["events"]
    # A server/proxy can deliver a complete message in one late chunk. OSIA
    # forwards it once when received; no artificial earlier events are created.
    stream.observe(delta("I will inspect the folder.", calls=[native()]))
    assert len(trace.snapshot()["events"]) == 1
    stream.finish()
    assert len(trace.snapshot()["events"]) == 1


def test_private_blocks_fragmented_markup_and_multiple_native_calls():
    stream, trace = projector()
    stream.observe(delta("<thi", calls=[native(arguments="{}"), native(index=1, identity="call2")]))
    assert not trace.snapshot()["events"]
    stream.observe(delta("nk>private deliberation</think>I will inspect the folder."))
    assert trace.snapshot()["events"][0]["summary"] == "I will inspect the folder."
    stream.finish()
    assert len(stream.message.tool_calls) == 2
    assert "private" not in str(trace.snapshot())
    unsafe, unsafe_trace = projector()
    unsafe.observe(delta("Bearer sensitive-value", calls=[native()]))
    unsafe.finish()
    assert not unsafe_trace.snapshot()["events"]


def test_typed_mistral_public_chunks_do_not_stringify_private_or_unknown_fields():
    assert public_assistant_text([
        {"type": "thinking", "thinking": [{"type": "text", "text": "private"}]},
        {"type": "text", "text": "Public words."},
        {"type": "unknown", "text": "private"},
    ]) == "Public words."


def test_actual_sdk_sse_native_call_assembly_preserves_public_text(monkeypatch):
    frames = []
    agent = OSIAAgent(lang="en")
    agent.provider, agent.model = "deepseek", "deepseek-flash"
    agent.configure_execution_trace(conversation_id="conv_v3", request_id="req_v3", turn_id="turn_v3",
                                    provider_id=agent.provider, model_id=agent.model, callback=frames.append)
    chunks = [
        {"content": "I will inspect the folder."},
        {"reasoning_content": "private chain of thought"},
        {"tool_calls": [{"index": 0, "id": "call1", "type": "function", "function": {"name": "list_files", "arguments": '{"path":'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"fixture"}'}}]},
    ]
    def handler(request):
        body = json.loads(request.content)
        assert body["stream"] is True and body["model"] == "deepseek-flash"
        wire = "".join("data: " + json.dumps({"id": "response1", "object": "chat.completion.chunk", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "delta": value, "finish_reason": None}]}) + "\n\n" for value in chunks)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire + "data: [DONE]\n\n")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with OpenAI(api_key="test-only", base_url="https://fixture.invalid/v1", http_client=http) as client:
            agent.client = client
            monkeypatch.setattr(agent, "_reasoning_request", lambda: {"kwargs": {}, "diagnostic": {}})
            content = agent._stream_with_sentences(lambda _: None)
    assert content == "I will inspect the folder."
    assert frames[0]["summary"] == content
    assert "private" not in str(frames)
    assert _extract_tool_calls(content, agent._streamed_provider_message, "deepseek") == [{"tool": "list_files", "path": "fixture"}]


def test_canonical_run_streams_commentary_without_final_sentence_consumer(monkeypatch, tmp_path):
    from osia import agent as module
    agent = OSIAAgent(lang="en")
    agent.provider, agent.model = "deepseek", "deepseek-flash"
    frames, order, requests = [], [], []
    agent.configure_execution_trace(conversation_id="conv_v3", request_id="req_v3", turn_id="turn_v3",
                                    provider_id=agent.provider, model_id=agent.model, callback=frames.append)
    monkeypatch.setattr(agent, "_resolve_route", lambda _: ("medium", []))
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path))
    monkeypatch.setitem(module.TOOL_MAP, "list_files", lambda path: order.append("tool") or "two files")
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *a, **k: None)
    monkeypatch.setattr("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False)
    def create(**kwargs):
        requests.append(kwargs)
        assert kwargs.get("stream") is True
        def response():
            if len(requests) == 1:
                yield NS(choices=[NS(delta=delta("I will inspect the folder.", [native()]), finish_reason=None)])
                assert frames[-1]["summary"] == "I will inspect the folder."
                order.append("live_before_response_end")
            else:
                yield NS(choices=[NS(delta=delta("Two files are present."), finish_reason="stop")])
        return response()
    agent.client = NS(chat=NS(completions=NS(create=create)))
    result = agent.run_terminal_turn("Please answer in English. Please list the files in the fixture directory and tell me how many you found.",
                                     turn_id="turn_v3", history=[], on_sentence=None)
    assert order == ["live_before_response_end", "tool"]
    assert result == "Two files are present."
    assert len(requests) == 2
    assert [e["summary"] for e in agent._execution_trace_ledger.snapshot()["events"] if "summary" in e] == ["I will inspect the folder."]


def test_codex_phase_commentary_is_public_live_and_separate_from_final():
    client = AppServerClient()
    attempt = _TurnAttempt(thread_id="native_thread", turn_id="native_turn", osia_turn_id="turn_v3",
                           conversation_id="conv_v3", client_request_id="req_v3", model_id="gpt-6-luna")
    client._attempts[attempt.thread_id] = attempt
    correlation = {"threadId": attempt.thread_id, "turnId": attempt.turn_id}
    def item(method, identity, phase, text=""):
        client._dispatch_notification(method, {**correlation,
            "item": {"type": "agentMessage", "id": identity, "phase": phase, "text": text}})
    item("item/started", "comment1", "commentary")
    client._dispatch_notification("item/agentMessage/delta", {**correlation, "itemId": "comment1", "delta": "I will inspect the folder."})
    category, payload = attempt.events.get_nowait()
    assert category == "public_reasoning_summary"
    assert payload["native_item_id"] == "comment1"
    assert payload["summary"] == "I will inspect the folder."
    assert not attempt.text_parts  # Before response completion, already public.
    item("item/completed", "comment1", "commentary", payload["summary"])
    assert attempt.events.empty()
    item("item/started", "final1", "final_answer")
    client._dispatch_notification("item/agentMessage/delta", {**correlation, "itemId": "final1", "delta": "I inspected the folder. Here is the result."})
    item("item/completed", "final1", "final_answer", "I inspected the folder. Here is the result.")
    assert "".join(attempt.text_parts) == "I inspected the folder. Here is the result."
    assert attempt.events.get_nowait()[0] == "delta"
    assert attempt.events.empty()
    assert attempt.final_output_item_id == "final1"


def test_codex_summary_and_public_message_items_have_separate_trace_sources():
    agent = OSIAAgent(lang="en")
    agent.provider, agent.model = "chatgpt_account", "gpt-6-luna"
    agent.configure_execution_trace(conversation_id="conv_v3", request_id="req_v3", turn_id="turn_v3",
                                    provider_id=agent.provider, model_id=agent.model)
    payload = {**{k: v for k, v in agent._execution_trace_ledger.binding_dict().items() if k != "effective_reasoning"},
               "native_thread_id": "native_thread", "native_turn_id": "native_turn", "summary": "Provider summary."}
    agent._emit_provider_public_reasoning_summary(payload)
    agent._emit_provider_public_reasoning_summary({**payload, "native_item_id": "comment1", "summary": "I will inspect the folder."})
    agent._emit_provider_public_reasoning_summary({**payload, "native_item_id": "comment2", "summary": "The listing arrived."})
    agent._emit_provider_public_reasoning_summary({**payload, "turn_id": "stale", "native_item_id": "comment2", "summary": "Stale."})
    assert [e["summary"] for e in agent._execution_trace_ledger.snapshot()["events"]] == [
        "Provider summary.", "I will inspect the folder.", "The listing arrived."]


def test_codex_public_seed_and_small_deltas_do_not_wait_for_completion():
    client = AppServerClient()
    attempt = _TurnAttempt(thread_id="native_thread", turn_id="native_turn")
    correlation = {"itemId": "summary1", "summaryIndex": 0}
    client._capture_public_reasoning_summary(attempt, "item/reasoning/summaryPartAdded", {
        **correlation, "part": {"type": "summary_text", "text": "I will inspect "},
    })
    assert attempt.events.get_nowait()[1]["summary"] == "I will inspect"
    client._capture_public_reasoning_summary(attempt, "item/reasoning/summaryTextDelta", {
        **correlation, "delta": "the folder.",
    })
    assert attempt.events.get_nowait()[1]["summary"] == "I will inspect the folder."
    client._capture_public_reasoning_summary(attempt, "item/reasoning/summaryTextDelta", {
        **correlation, "delta": " Next",
    })
    assert attempt.events.get_nowait()[1]["summary"] == "I will inspect the folder. Next"
