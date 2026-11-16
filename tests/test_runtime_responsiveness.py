"""Runtime speedups preserve tool availability, streaming safety and context."""
from dataclasses import replace
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from osia import agent, tool_contracts
from osia.public_commentary import PublicCommentaryStream
from osia.conversation_context import build_context


def _subject(chunks):
    subject = object.__new__(agent.OSIAAgent)
    subject._stream_deltas = lambda: iter(chunks)
    return subject


def test_complete_sentence_is_visible_before_provider_continues():
    emitted = []

    def chunks():
        yield "Bonjour. "
        assert emitted == ["Bonjour."]
        yield "La suite arrive ensuite."

    subject = _subject(chunks())
    assert subject._stream_with_sentences(emitted.append) == "Bonjour. La suite arrive ensuite."
    assert emitted == ["Bonjour.", "La suite arrive ensuite."]


def test_early_clause_uses_later_separator_after_short_opening():
    emitted = []
    first = "Oui, voici une explication suffisamment longue pour commencer, "

    def chunks():
        yield first
        assert emitted == [first.strip()]
        yield "et sa suite."

    _subject(chunks())._stream_with_sentences(emitted.append)
    assert emitted == [first.strip(), "et sa suite."]


def test_fragmented_decimal_is_not_split_at_temporary_end_of_delta():
    emitted = []
    _subject(["La version est 1.", "25 et fonctionne. ", "Fin."])._stream_with_sentences(emitted.append)
    assert emitted == ["La version est 1.25 et fonctionne.", "Fin."]


@pytest.mark.parametrize("hidden", [
    '<think>PRIVATE THOUGHT. Never public.</think>',
    '<thinking>PRIVATE THOUGHT. Never public.</thinking>',
    '<tool_call>{"tool":"read_file","path":"PRIVATE ARGUMENT"}</tool_call>',
    '<tool_code>print(read_file(path="PRIVATE ARGUMENT"))</tool_code>',
    '<｜｜DSML｜｜calls><｜｜DSML｜｜invoke name="read_file"><｜｜DSML｜｜parameter name="path" string="true">PRIVATE ARGUMENT</｜｜DSML｜｜parameter></｜｜DSML｜｜invoke></｜｜DSML｜｜calls>',
    '<  |DSML| invoke name="read_file">{"path":"PRIVATE ARGUMENT"}</ |DSML| invoke>',
])
def test_every_markup_split_keeps_private_protocol_out_of_public_stream(hidden):
    prefix = "Je regarde. "
    suffix = "Suite visible." if hidden.startswith(("<think>", "<thinking>")) else "UNEXECUTED CLAIM."
    for split in range(1, len(hidden)):
        emitted = []
        full = _subject([prefix + hidden[:split], hidden[split:] + suffix])._stream_with_sentences(emitted.append)
        assert full == prefix + hidden + suffix
        public = " ".join(emitted)
        assert "PRIVATE" not in public, (split, public)
        assert "DSML" not in public, (split, public)
        assert "UNEXECUTED" not in public, (split, public)
        assert "Je regarde." in public
        if suffix == "Suite visible.":
            assert "Suite visible." in public


def test_argument_only_native_deltas_do_not_resanitize_unchanged_public_text():
    clean = Mock(side_effect=lambda text: text)
    emitted = []
    stream = PublicCommentaryStream(provider="test", model="test", turn_id="t", round_id="r", parse=lambda *_: [], clean=clean, emit=lambda *value: emitted.append(value))
    stream.observe(SimpleNamespace(content="Reading.", tool_calls=[]))
    for index in range(100):
        stream.observe(SimpleNamespace(content=None, tool_calls=[SimpleNamespace(index=0, id="c" if index == 0 else None, function=SimpleNamespace(name="read_file" if index == 0 else None, arguments="x"))]))
    stream.finish()
    assert clean.call_count == 2  # initial tool context and complete sanitization
    assert emitted == [("Reading.", "r")]
    assert stream.message.tool_calls[0].function.arguments == "x" * 100


def test_plain_text_deltas_do_not_reparse_the_entire_history_each_time():
    parse = Mock(return_value=[])
    stream = PublicCommentaryStream(provider="test", model="test", turn_id="t", round_id="r", parse=parse, clean=lambda text: text, emit=lambda *_: pytest.fail("No tool context"))
    for _ in range(100):
        stream.observe(SimpleNamespace(content="Ordinary response. ", tool_calls=[]))
    stream.finish()
    assert parse.call_count <= 1
    assert stream.message.content == "Ordinary response. " * 100


def test_static_prompt_reuses_rendering_but_observes_schema_edits(monkeypatch):
    spec = replace(tool_contracts.STATIC_TOOL_SPECS[0], name="responsiveness_fixture", input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False})
    monkeypatch.setattr(tool_contracts, "effective_static_tool_specs", lambda: (spec,))
    bundle = Mock(wraps=tool_contracts._prompt_schema_bundle)
    monkeypatch.setattr(tool_contracts, "_prompt_schema_bundle", bundle)
    first = tool_contracts.static_tool_prompt_block()
    for _ in range(5):
        assert tool_contracts.static_tool_prompt_block() == first
    assert bundle.call_count == 1
    spec.input_schema["properties"]["path"]["enum"] = ["new"]
    updated = tool_contracts.static_tool_prompt_block()
    assert updated != first and '"enum":["new"]' in updated
    assert bundle.call_count == 2


def test_static_prompt_follows_current_inventory_and_contracts(monkeypatch):
    original = tool_contracts.STATIC_TOOL_SPECS[0]
    current = [replace(original, name="fixture_first")]
    monkeypatch.setattr(tool_contracts, "effective_static_tool_specs", lambda: tuple(current))
    first = tool_contracts.static_tool_prompt_block()
    current[:] = [replace(original, name="fixture_second", capability="New capability")]
    second = tool_contracts.static_tool_prompt_block()
    assert "fixture_first(" not in second and "fixture_second(" in second
    assert "New capability" in second
    current.clear()
    empty = tool_contracts.static_tool_prompt_block()
    assert "fixture_first(" not in empty and "fixture_second(" not in empty
    current.append(replace(original, name="fixture_first"))
    assert tool_contracts.static_tool_prompt_block() == first


@pytest.mark.parametrize("provider,force_compaction", [("deepseek", False), ("deepseek", True), ("chatgpt_account", False)])
def test_context_copies_wire_values_without_mutating_canonical_history(provider, force_compaction):
    messages = [{"role": "system", "content": "Keep the requested objective."}]
    for index in range(30):
        messages.extend([
            {"role": "user", "content": f"Question {index}: original request."},
            {"role": "assistant", "content": "Original answer."},
        ])
    messages.extend([
        {"role": "user", "content": [{"type": "text", "text": "Inspect this image."}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"fixture"}'}}]},
        {"role": "tool", "content": "Observed contents.", "tool_call_id": "c"},
    ])
    original = copy.deepcopy(messages)
    kwargs = dict(provider=provider, model="fixture", context_limit=32768, force_compaction=force_compaction)
    result = build_context(messages, **kwargs)
    image = next(message for message in result.messages if isinstance(message["content"], list))
    image["content"][1]["image_url"]["url"] = "changed by caller"
    call = next(message for message in result.messages if message.get("tool_calls"))
    call["tool_calls"][0]["function"]["arguments"] = "changed by caller"
    assert messages == original
    again = build_context(messages, **kwargs)
    assert "changed by caller" not in repr(again.messages)
