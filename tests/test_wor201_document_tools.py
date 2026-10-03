from __future__ import annotations

import json
import subprocess
import threading
import types
from concurrent.futures import ThreadPoolExecutor

import pypdfium2
import pytest

from osia import agent as agent_module
from osia import document_artifacts, document_reader, pdf_renderer
from osia.agent import OSIAAgent
from osia.document_sources import DocumentSourceStore, set_source_store_for_testing
from osia.tools import desktop_control
from osia.tool_contracts import (
    ExecutionOutcome,
    ToolValidationError,
    get_tool_spec,
    static_tool_prompt_block,
    validate_arguments,
)


DESKTOP_PDF = r"C:\Users\fixture\Desktop\WOR201-document-e2e.pdf"
PDF_BLOCKS = [
    {"type": "paragraph", "text": "ALPHA-27"},
    {
        "type": "table",
        "headers": ["Component", "Status"],
        "rows": [["Relay", "Healthy"], ["Documents", "Ready"]],
    },
    {"type": "page_break"},
    {"type": "heading", "text": "Layout Check", "level": 1},
    {"type": "paragraph", "text": "OMEGA-91"},
    {
        "type": "paragraph",
        "text": "This paragraph confirms that the second page is distinct.",
    },
    {"type": "paragraph", "text": "Synthetic WOR-201 fixture"},
]


@pytest.fixture
def private_document_stores(tmp_path):
    artifacts = document_artifacts.ArtifactStore(tmp_path / "artifacts")
    sources = DocumentSourceStore(tmp_path / "sources")
    previous_artifacts = document_artifacts.set_store_for_testing(artifacts)
    previous_sources = set_source_store_for_testing(sources)
    try:
        yield artifacts, sources
    finally:
        document_artifacts.set_store_for_testing(previous_artifacts)
        set_source_store_for_testing(previous_sources)


def _install_stream_rounds(agent, rounds):
    remaining = iter(rounds)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        content = next(remaining)
        if on_first_token is not None:
            on_first_token()
        callback(content)
        return content

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def _document_reference(document_id: str) -> str:
    return json.dumps({
        "artifact_id": document_id,
        "document_type": "pdf",
        "page_count": 2,
        "content_length": 256,
        "supported_queries": ["pages", "search"],
        "expires_at": 4_000_000_000,
    })


def _single_flight_policy_call(single_flight, tool_name, arguments):
    validated = validate_arguments(
        get_tool_spec(tool_name).input_schema,
        arguments,
    )
    return single_flight.execute(
        tool_name,
        validated,
        lambda normalized: agent_module._execute_tool_with_policy(
            tool_name,
            normalized,
            allow_retry=False,
            lang="en",
        ),
    )


def test_same_turn_duplicate_read_document_single_flight_fans_out_and_final(
    monkeypatch,
):
    document_id = "doc_" + "d" * 32
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: dispatches.append(dict(kwargs))
        or _document_reference(document_id),
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )

    calls = (
        '<tool_call>{"tool":"read_document","path":"'
        'C:\\\\Synthetic\\\\same.pdf"}</tool_call>'
    )
    feedback_frames = []
    rounds = iter([calls + calls, "One canonical final response."])
    agent = OSIAAgent(lang="en")

    def capture_stream(self, callback, *, on_first_token=None, guard=None):
        feedback_frames.extend(
            str(message.get("content") or "")
            for message in self.messages
            if str(message.get("content") or "").startswith("<tool_result>")
        )
        content = next(rounds)
        if on_first_token is not None:
            on_first_token()
        callback(content)
        return content

    agent._stream_with_sentences = types.MethodType(capture_stream, agent)
    visible = []
    final = agent.run("Read the synthetic PDF.", on_sentence=visible.append)

    assert dispatches == [{"path": r"C:\Synthetic\same.pdf"}]
    assert final == "One canonical final response."
    assert visible == [final]
    assert len(feedback_frames) == 1
    assert feedback_frames[0].count("Tool read_document result:") == 2
    assert feedback_frames[0].count(document_id) == 2

    # Result fan-out is per provider call identity; the identity never enters
    # the canonical execution key or the dispatcher's validated arguments.
    single_flight = agent_module._SameTurnDocumentSingleFlight()
    provider_results = {}
    for tool_call_id in ("call_read_a", "call_read_b"):
        record, _reused, _normalized = _single_flight_policy_call(
            single_flight,
            "read_document",
            {"path": r"C:\Synthetic\identity.pdf"},
        )
        provider_results[tool_call_id] = record.result
    assert set(provider_results) == {"call_read_a", "call_read_b"}
    assert provider_results["call_read_a"] == provider_results["call_read_b"]
    assert json.loads(provider_results["call_read_a"])["artifact_id"] == document_id
    assert dispatches[-1] == {"path": r"C:\Synthetic\identity.pdf"}
    assert len(dispatches) == 2


def test_same_turn_duplicate_query_document_dispatches_once(monkeypatch):
    document_id = "doc_" + "e" * 32
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "query_document",
        lambda **kwargs: dispatches.append(dict(kwargs))
        or '{"ok":true,"items":[{"snippet":"OMEGA-91"}]}',
    )
    single_flight = agent_module._SameTurnDocumentSingleFlight()
    provider_results = {}
    for tool_call_id, max_chars in (
        ("call_query_a", 12_000),
        ("call_query_b", 8_000),
    ):
        record, reused, normalized = _single_flight_policy_call(
            single_flight,
            "query_document",
            {
                "document_id": document_id,
                "operation": "search",
                "query": "OMEGA-91",
                "max_chars": max_chars,
            },
        )
        provider_results[tool_call_id] = record.result
        assert normalized["max_chars"] == 8_000
        assert reused is (tool_call_id == "call_query_b")

    assert dispatches == [{
        "document_id": document_id,
        "operation": "search",
        "query": "OMEGA-91",
        "max_chars": 8_000,
    }]
    assert set(provider_results) == {"call_query_a", "call_query_b"}
    assert provider_results["call_query_a"] == provider_results["call_query_b"]


def test_document_single_flight_separates_arguments_and_turns(monkeypatch):
    document_id = "doc_" + "f" * 32
    read_dispatches = []
    query_dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: read_dispatches.append(dict(kwargs))
        or _document_reference(document_id),
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "query_document",
        lambda **kwargs: query_dispatches.append(dict(kwargs)) or '{"ok":true}',
    )
    first_turn = agent_module._SameTurnDocumentSingleFlight()
    for path in (r"C:\Synthetic\one.pdf", r"C:\Synthetic\two.pdf"):
        _single_flight_policy_call(first_turn, "read_document", {"path": path})
    for query in ("ALPHA-27", "OMEGA-91"):
        _single_flight_policy_call(first_turn, "query_document", {
            "document_id": document_id,
            "operation": "search",
            "query": query,
        })
    assert len(read_dispatches) == 2
    assert len(query_dispatches) == 2

    second_turn = agent_module._SameTurnDocumentSingleFlight()
    _single_flight_policy_call(
        second_turn,
        "read_document",
        {"path": r"C:\Synthetic\one.pdf"},
    )
    assert len(read_dispatches) == 3


def test_document_single_flight_excludes_other_and_preserves_unknown(monkeypatch):
    calls = []
    single_flight = agent_module._SameTurnDocumentSingleFlight()
    for tool_name in ("render_pdf", "synthetic_side_effect"):
        for _ in range(2):
            record, reused, normalized = single_flight.execute(
                tool_name,
                {"value": 1},
                lambda arguments, name=tool_name: calls.append((name, arguments))
                or object(),
            )
            assert record is not None
            assert reused is False
            assert normalized == {"value": 1}
    assert [name for name, _arguments in calls] == [
        "render_pdf", "render_pdf",
        "synthetic_side_effect", "synthetic_side_effect",
    ]

    unknown_calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: unknown_calls.append(dict(kwargs))
        or "ERROR: synthetic ambiguous document failure",
    )
    unknown_single_flight = agent_module._SameTurnDocumentSingleFlight()
    for _ in range(2):
        unknown, reused, _normalized = _single_flight_policy_call(
            unknown_single_flight,
            "read_document",
            {"path": r"C:\Synthetic\unknown.pdf"},
        )
        assert unknown.outcome == ExecutionOutcome.OUTCOME_UNKNOWN
        assert unknown.dispatched is True
        assert reused is False
    assert len(unknown_calls) == 2

    terminal_calls = []
    terminal_error = (
        'ERROR: {"category":"resource_limit_exceeded",'
        '"limit_kind":"page_count","ok":false}'
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: terminal_calls.append(dict(kwargs)) or terminal_error,
    )
    terminal_single_flight = agent_module._SameTurnDocumentSingleFlight()
    terminal_records = [
        _single_flight_policy_call(
            terminal_single_flight,
            "read_document",
            {"path": r"C:\Synthetic\two-pages.pdf", "max_pages": 1},
        )
        for _ in range(2)
    ]
    assert len(terminal_calls) == 1
    assert [reused for _record, reused, _normalized in terminal_records] == [
        False, True,
    ]
    for record, _reused, _normalized in terminal_records:
        assert record.result == terminal_error
        assert record.outcome == ExecutionOutcome.CONFIRMED_FAILURE_BEFORE_EFFECT
        assert record.timed_out is False
    assert terminal_records[0][0].dispatched is True
    assert terminal_records[0][0].attempts == 1
    assert terminal_records[1][0].dispatched is False
    assert terminal_records[1][0].attempts == 0

    rejected = agent_module._execute_tool_with_policy(
        "read_document", {"path": "relative.pdf"}, lang="en",
    )
    assert rejected.outcome == ExecutionOutcome.REJECTED_BEFORE_EXECUTION
    assert rejected.dispatched is False
    assert rejected.result == "ERROR: invalid tool arguments; the tool was not executed"
    assert all(fallback not in rejected.result.lower() for fallback in (
        "shell", "bash", "writer_pdf",
    ))


def test_concurrent_duplicate_document_calls_share_one_in_flight_execution(
    monkeypatch,
):
    document_id = "doc_" + "1" * 32
    dispatch_count = 0
    dispatch_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def blocking_read(**_kwargs):
        nonlocal dispatch_count
        with dispatch_lock:
            dispatch_count += 1
        started.set()
        assert release.wait(timeout=2)
        return _document_reference(document_id)

    monkeypatch.setitem(agent_module.TOOL_MAP, "read_document", blocking_read)
    single_flight = agent_module._SameTurnDocumentSingleFlight()
    arguments = {"path": r"C:\Synthetic\concurrent.pdf"}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            _single_flight_policy_call,
            single_flight,
            "read_document",
            arguments,
        )
        assert started.wait(timeout=2)
        second = executor.submit(
            _single_flight_policy_call,
            single_flight,
            "read_document",
            arguments,
        )
        release.set()
        first_record, first_reused, _ = first.result(timeout=3)
        second_record, second_reused, _ = second.result(timeout=3)

    assert dispatch_count == 1
    assert sorted((first_reused, second_reused)) == [False, True]
    assert first_record.result == second_record.result
    assert json.loads(first_record.result)["artifact_id"] == document_id


def test_read_document_path_contract_is_canonical_precise_and_single_dispatch(
    monkeypatch,
):
    spec = get_tool_spec("read_document")
    arguments = {"path": DESKTOP_PDF, "language": "fr"}
    assert validate_arguments(spec.input_schema, arguments) == arguments

    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: calls.append(dict(kwargs)) or '{"ok":true}',
    )
    record = agent_module._execute_tool_with_policy(
        "read_document", arguments, allow_retry=True, lang="en",
    )
    assert calls == [arguments]
    assert record.dispatched is True
    assert record.attempts == 1
    assert record.outcome == ExecutionOutcome.SUCCESS_VERIFIED

    for invalid in ({}, {"file_path": DESKTOP_PDF}, {"path": "relative.pdf"}):
        rejected = agent_module._execute_tool_with_policy(
            "read_document", invalid, allow_retry=True, lang="en",
        )
        assert rejected.dispatched is False
        assert rejected.outcome == ExecutionOutcome.REJECTED_BEFORE_EXECUTION
        assert rejected.result == (
            "ERROR: invalid tool arguments; the tool was not executed"
        )
        assert "unavailable" not in rejected.result.lower()


def test_read_document_never_guesses_a_relative_path(monkeypatch):
    monkeypatch.setattr(
        document_reader,
        "read_document",
        lambda *_args, **_kwargs: pytest.fail("relative path reached document I/O"),
    )
    result = agent_module.tool_read_document(path="relative-document.pdf")
    assert result == 'ERROR: {"category":"absolute_path_required","ok":false}'


def test_query_document_normalizes_requested_budget_to_store_cap(monkeypatch):
    document_id = "doc_" + "b" * 32
    arguments = {
        "document_id": document_id,
        "operation": "pages",
        "max_items": 10,
        "max_chars": 12_000,
    }
    assert validate_arguments(
        get_tool_spec("query_document").input_schema, arguments,
    ) == arguments
    calls = []
    monkeypatch.setattr(
        document_reader,
        "query_document",
        lambda *args, **kwargs: calls.append((args, kwargs)) or '{"ok":true}',
    )
    assert agent_module.tool_query_document(**arguments) == '{"ok":true}'
    assert calls == [(
        (document_id, "pages"),
        {
            "query": None, "cursor": None, "max_items": 10,
            "max_chars": 8_000, "section_id": None, "table_id": None,
            "sheet_id": None, "cell_range_ref": None, "slide_id": None,
            "embedded_artifact_id": None, "frame_id": None,
            "track_id": None, "start_seconds": None, "end_seconds": None,
        },
    )]


def test_same_pdf_followup_reuses_private_reference_without_reread(monkeypatch):
    document_id = "doc_" + "c" * 32
    reference = {
        "schema_version": 2,
        "artifact_id": document_id,
        "document_id": document_id,
        "document_type": "pdf",
        "format_label": "pdf",
        "document_family": "document",
        "status": "complete",
        "page_count": 2,
        "frame_count": 0,
        "track_count": 0,
        "segment_count": 0,
        "chapter_count": 0,
        "section_count": 2,
        "table_count": 1,
        "sheet_count": 0,
        "slide_count": 0,
        "chunk_count": 2,
        "embedded_count": 0,
        "page_methods": ["text", "text"],
        "content_length": 128,
        "confidence": 1.0,
        "duration_seconds": None,
        "width": 0,
        "height": 0,
        "provenance_available": True,
        "truncated": False,
        "warnings": [],
        "supported_queries": ["search", "pages", "table"],
        "resumable": False,
        "expires_at": 4_000_000_000,
    }
    read_calls = []
    query_calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: read_calls.append(dict(kwargs)) or json.dumps(reference),
    )
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "query_document",
        lambda **kwargs: query_calls.append(dict(kwargs))
        or '{"ok":true,"items":[{"snippet":"OMEGA-91"}]}',
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="fr")
    _install_stream_rounds(agent, [
        '<tool_call>{"tool":"read_document","path":"'
        'C:\\\\Users\\\\fixture\\\\Desktop\\\\WOR201-document-e2e.pdf"}</tool_call>',
        "Le PDF a été lu.",
    ])
    first = agent.run(
        "Vérifie ce PDF.", on_sentence=lambda _text: None,
        current_conversation_id="wor201-conversation",
    )
    assert first == "Le PDF a été lu."
    assert read_calls == [{"path": DESKTOP_PDF}]

    captured_contexts = []
    rounds = iter([
        '<tool_call>{"tool":"query_document","document_id":"'
        + document_id
        + '","operation":"search","query":"OMEGA-91"}</tool_call>',
        "OMEGA-91",
    ])

    def capture_stream(self, callback, *, on_first_token=None, guard=None):
        captured_contexts.extend(
            message["content"] for message in self.messages
            if message.get("_document_context")
        )
        content = next(rounds)
        if on_first_token is not None:
            on_first_token()
        callback(content)
        return content

    agent._stream_with_sentences = types.MethodType(capture_stream, agent)
    second = agent.run(
        "Dans ce même PDF, quel marqueur apparaît sur la deuxième page ?",
        history=[
            {"role": "user", "content": "Vérifie ce PDF."},
            {"role": "assistant", "content": first},
        ],
        on_sentence=lambda _text: None,
        current_conversation_id="wor201-conversation",
    )
    assert second == "OMEGA-91"
    assert read_calls == [{"path": DESKTOP_PDF}]
    assert query_calls == [{
        "document_id": document_id,
        "operation": "search",
        "query": "OMEGA-91",
    }]
    assert len(set(captured_contexts)) == 1
    assert document_id in captured_contexts[0]
    assert "call query_document exactly once" in captured_contexts[0]
    assert "do not call read_document again" in captured_contexts[0]
    assert DESKTOP_PDF not in captured_contexts[0]


def test_same_pdf_followup_authorizes_prior_private_artifact(
    tmp_path, private_document_stores, monkeypatch,
):
    source = tmp_path / "same-document.txt"
    source.write_text(
        "WOR-201 Document E2E\nFirst page ALPHA-27\nSecond page OMEGA-91\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="fr")
    first_call = (
        '<tool_call>{"tool":"read_document","path":'
        + json.dumps(str(source))
        + "}</tool_call>"
    )
    _install_stream_rounds(agent, [first_call, "Le document a été lu."])
    first = agent.run(
        "Vérifie ce document.",
        on_sentence=lambda _text: None,
        current_conversation_id="wor201-private-followup",
    )
    assert first == "Le document a été lu."
    assert len(agent.last_run_artifacts) == 1
    document_id = agent.last_run_artifacts[0]["document_id"]

    second_call = (
        '<tool_call>{"tool":"query_document","document_id":"'
        + document_id
        + '","operation":"search","query":"OMEGA-91"}</tool_call>'
    )
    _install_stream_rounds(agent, [second_call, "OMEGA-91"])
    second = agent.run(
        "Dans ce même document, quel marqueur apparaît ensuite ?",
        history=[
            {"role": "user", "content": "Vérifie ce document."},
            {"role": "assistant", "content": first},
        ],
        on_sentence=lambda _text: None,
        current_conversation_id="wor201-private-followup",
    )
    assert second == "OMEGA-91"
    tool_feedback = "\n".join(
        str(message.get("content") or "")
        for message in agent.messages
        if str(message.get("content") or "").startswith("<tool_result>")
    )
    assert "OMEGA-91" in tool_feedback
    assert "unauthorized_artifact" not in tool_feedback


def test_fast_command_failure_preserves_streams_and_exit_code(monkeypatch):
    monkeypatch.setattr(agent_module.sys, "platform", "win32")
    monkeypatch.setattr(agent_module, "_is_long_running_command", lambda _c: False)
    monkeypatch.setattr(
        agent_module,
        "_run_windows_powershell",
        lambda _command, _timeout: subprocess.CompletedProcess(
            args=["powershell"], returncode=7,
            stdout="partial output\n", stderr="controlled failure\n",
        ),
    )
    result = agent_module.tool_bash("synthetic failure", timeout=10)
    assert result == (
        "STDOUT:\npartial output\n\n"
        "STDERR:\ncontrolled failure\n\n[exit code 7]"
    )


def test_windows_app_resolver_decodes_powershell_defensively(monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="[]")

    monkeypatch.setattr(desktop_control.subprocess, "run", fake_run)
    assert desktop_control._resolve_uwp("synthetic-app") is None
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"


def test_dedicated_document_routing_has_one_final_and_no_shell_fallback(monkeypatch):
    description = get_tool_spec("read_document").capability
    windows_example = r'{"path":"C:\\Users\\Example\\Desktop\\report.pdf"}'
    assert windows_example in description
    assert '"path":"<value>"' not in description
    assert "max_pages is only a whole-document safety ceiling" in description

    prompt = static_tool_prompt_block("en")
    read_line = next(
        line for line in prompt.splitlines()
        if line.startswith("- read_document(")
    )
    assert "(path:string; optional:source_ref" in read_line
    # The Windows example and the whole-document ceiling live in the tool
    # capability text (asserted above); the prompt line is the compact schema
    # summary since WOR-300 and is truncated deterministically.
    assert "never shell" in read_line
    assert "authorized_document_source" in read_line

    query_description = get_tool_spec("query_document").capability
    assert "pages lists rendered page-frame metadata" in query_description
    query_line = next(
        line for line in prompt.splitlines()
        if line.startswith("- query_document(")
    )
    assert "authorized_live_artifact" in query_line

    calls = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "read_document",
        lambda **kwargs: calls.append(dict(kwargs)) or '{"ok":true}',
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    agent = OSIAAgent(lang="en")
    _install_stream_rounds(agent, [
        '<tool_call>{"tool":"read_document","path":"'
        'C:\\\\Users\\\\fixture\\\\Desktop\\\\WOR201-document-e2e.pdf"}</tool_call>',
        "The dedicated document result produced this single final response.",
        # WOR-303: one canonical continuation round when evidence is missing.
        "The dedicated document result produced this single final response.",
    ])
    visible = []
    final = agent.run(
        "Check the existing PDF on my Desktop.", on_sentence=visible.append,
    )
    assert calls == [{"path": DESKTOP_PDF}]
    assert final == visible[0]
    assert visible == [final]

    calls.clear()
    rejected = agent_module._execute_tool_with_policy(
        "read_document", {"file_path": "document.pdf"}, lang="en",
    )
    assert calls == []
    assert rejected.dispatched is False
    assert "invalid tool arguments" in rejected.result
    assert "writer_pdf" not in rejected.result
    assert "bash" not in rejected.result.lower()


def test_two_page_pdf_render_read_and_targeted_query_chain(
    tmp_path, private_document_stores, monkeypatch,
):
    monkeypatch.setattr(
        pdf_renderer, "resolve_destination", lambda _label: ("Desktop", tmp_path),
    )
    receipt = json.loads(pdf_renderer.render_pdf(
        title="WOR-201 Document E2E",
        filename="WOR201-document-e2e.pdf",
        destination="desktop",
        page_size="A4",
        blocks=PDF_BLOCKS,
    ))
    assert receipt == {
        "byte_size": receipt["byte_size"],
        "destination": "Desktop",
        "filename": "WOR201-document-e2e.pdf",
        "page_count": 2,
        "status": "success_verified",
        "verified": True,
    }
    assert set(receipt) == {
        "byte_size", "destination", "filename", "page_count", "status", "verified",
    }

    output = tmp_path / receipt["filename"]
    pdf = pypdfium2.PdfDocument(str(output))
    try:
        assert len(pdf) == 2
        page_text = [
            pdf[index].get_textpage().get_text_range() for index in range(len(pdf))
        ]
        rendered = [pdf[index].render(scale=1).to_pil() for index in range(len(pdf))]
    finally:
        pdf.close()
    assert all(image.width > 500 and image.height > 700 for image in rendered)
    assert "WOR-201 Document E2E" in page_text[0]
    assert "ALPHA-27" in page_text[0]
    assert all(value in page_text[0] for value in ("Component", "Status", "Relay", "Healthy", "Documents", "Ready"))
    assert "Layout Check" in page_text[1]
    assert "OMEGA-91" in page_text[1]
    assert "second page is distinct" in page_text[1]
    assert "Synthetic WOR-201 fixture" in page_text[1]

    monkeypatch.setattr(document_reader, "_document_policy_epoch", lambda: 1)
    with document_artifacts.artifact_access_scope("wor201-owner"):
        reference = json.loads(document_reader.read_document(str(output)))
        assert reference["ok"] is True
        assert reference["page_count"] == 2

        monkeypatch.setattr(
            document_reader,
            "_read_snapshot_identity",
            lambda *_a, **_k: pytest.fail("query_document reread the source file"),
        )
        query = json.loads(document_reader.query_document(
            reference["document_id"], "search", query="OMEGA-91",
        ))
    assert query["ok"] is True
    assert query["not_found"] is False
    assert any("OMEGA-91" in item["snippet"] for item in query["items"])

    query_spec = get_tool_spec("query_document")
    with pytest.raises(ToolValidationError, match="invalid_arguments"):
        validate_arguments(query_spec.input_schema, {
            "document_id": reference["document_id"], "operation": "search",
        })
