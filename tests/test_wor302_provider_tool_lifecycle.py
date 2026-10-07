"""WOR-302 — provider tool-call lifecycle regression contract.

Deterministic coverage of the canonical lifecycle:

    provider chunk -> normalize -> detect -> parse -> dispatch -> receipt ->
    reinject -> same-provider continuation -> terminal only when the whole
    objective is complete/blocked.

The DSML grammar under test is the one captured live from DeepSeek V4.1
Flash: doubled fullwidth vertical bar (U+FF5C) around the ``DSML`` keyword,
``invoke name="TOOL"`` elements, ``parameter name="KEY"`` elements, an
optional ``calls`` wrapper, optional single-bar/ASCII-bar variants and
bare-JSON invoke bodies. Raw protocol must never reach the user projection.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent, TOOL_MAP, _extract_tool_calls
from osia.tool_contracts import ExecutionOutcome

_ROOT = Path(__file__).resolve().parents[1]
_BAR = "\uff5c\uff5cDSML\uff5c\uff5c"


def _dsml_invoke(name: str, bar: str = _BAR, **params) -> str:
    out = f'<{bar} invoke name="{name}">\n'
    for key, value in params.items():
        out += (
            f'<{bar} parameter name="{key}" string="true">'
            f"{value}</{bar} parameter>\n"
        )
    out += f"</{bar} invoke>\n"
    return out


def _dsml_response(*invokes: str) -> str:
    return f"<{_BAR} calls>\n" + "".join(invokes) + f"</{_BAR} calls>"


def _tool_call(tool: str, **kwargs) -> str:
    return "<tool_call>\n" + json.dumps({"tool": tool, **kwargs}) + "\n</tool_call>"


class _ScriptedCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        item = self.responses[index]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            message = SimpleNamespace(
                content=item.get("content"),
                tool_calls=item.get("tool_calls"),
            )
        else:
            message = SimpleNamespace(content=item, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")]
        )


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_ScriptedCompletions(responses))


def _agent(monkeypatch, responses, *, provider="deepseek",
           model="deepseek-flash", lang="fr"):
    agent = OSIAAgent(lang=lang)
    agent.provider = provider
    agent.model = model
    agent.client = _FakeClient(responses)
    monkeypatch.setattr(agent, "_resolve_route", lambda _text: ("medium", []))
    return agent


def test_legacy_run_wrapper_does_not_import_goal_contract_v2(monkeypatch):
    agent = _agent(monkeypatch, ["Bonjour."])
    real_import = __import__

    def reject_goal_contract_v2(name, *args, **kwargs):
        if name == "osia.goal_contract_v2":
            raise AssertionError("GoalContract V2 is not a runtime dependency")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=reject_goal_contract_v2):
        final = agent.run("Bonjour.", history=[])

    assert final == "Bonjour."
    assert len(agent.client.chat.completions.calls) == 1
    assert agent.client.chat.completions.calls[0]["model"] == agent.model


def test_canonical_turn_dispatches_and_reinjects_without_strategy(
    monkeypatch, tmp_path,
):
    dispatched = []
    _patch_tool(
        monkeypatch,
        "read_file",
        lambda path: dispatched.append(path) or "fixture file contents",
    )
    agent = _agent(
        monkeypatch,
        [
            _dsml_response(_dsml_invoke("read_file", path="C:\\Temp\\wor335.txt")),
            "The file says fixture file contents.",
        ],
    )
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path))

    with patch(
        "osia.execution_strategy.build_execution_strategy",
        side_effect=AssertionError("canonical Chat must not build a strategy"),
    ):
        final = agent.run_terminal_turn(
            "Read C:\\Temp\\wor335.txt and report its contents.",
            turn_id="wor335-canonical-tool-loop",
            history=[],
        )

    calls = agent.client.chat.completions.calls
    assert final == "The file says fixture file contents."
    assert dispatched == ["C:\\Temp\\wor335.txt"]
    assert len(calls) == 2
    assert all(call["model"] == agent.model for call in calls)
    assert any(
        "fixture file contents" in str(message.get("content") or "")
        for message in calls[1]["messages"]
    )

def _fake_stream(agent, items):
    remaining = iter(items)
    calls = []

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        calls.append(1)
        item = next(remaining)
        if isinstance(item, BaseException):
            raise item
        return item

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)
    return calls


def _install_fake_deltas(agent, chunks):
    def fake_deltas():
        for item in chunks:
            yield item

    agent._stream_deltas = fake_deltas


# ── 1-3, 6: parser grammar ─────────────────────────────────────────────────

def test_complete_dsml_bash_call_parses_exactly_once():
    text = _dsml_response(
        _dsml_invoke("bash", command="adb devices"),
    )
    calls = _extract_tool_calls(text, provider="deepseek")
    assert calls == [{"tool": "bash", "command": "adb devices"}]
    # Parsing is idempotent on the same payload.
    assert _extract_tool_calls(text, provider="deepseek") == calls


def test_dsml_prose_before_call_keeps_prose_and_internalizes_call():
    text = (
        "Je commence par creer le dossier sur le bureau.\n\n"
        + _dsml_response(_dsml_invoke(
            "create_directory", path="C:\\Users\\vertu\\Desktop\\Pixel9A",
        ))
    )
    calls = _extract_tool_calls(text, provider="deepseek")
    assert calls == [{
        "tool": "create_directory",
        "path": "C:\\Users\\vertu\\Desktop\\Pixel9A",
    }]
    display = agent_module.strip_tool_artifacts(text)
    assert display == "Je commence par creer le dossier sur le bureau."
    assert "DSML" not in display and "\uff5c" not in display


def test_multiple_dsml_calls_parse_in_emission_order():
    text = _dsml_response(
        _dsml_invoke("bash", command="where adb"),
        _dsml_invoke("list_files", path="C:\\Users\\vertu\\Desktop"),
        _dsml_invoke(
            "write_file",
            path="C:\\Users\\vertu\\Desktop\\a.txt",
            content="bonjour",
        ),
    )
    calls = _extract_tool_calls(text, provider="deepseek")
    assert [call["tool"] for call in calls] == ["bash", "list_files", "write_file"]


def test_dsml_preserves_windows_paths_quotes_newlines():
    content = 'ligne1 "guillemets"\nligne2 \\chemin\\finale'
    text = _dsml_response(_dsml_invoke(
        "write_file",
        path="C:\\Users\\vertu\\Desktop\\WOR302_TEST\\beta.txt",
        content=content,
    ))
    calls = _extract_tool_calls(text, provider="deepseek")
    assert calls[0]["path"] == "C:\\Users\\vertu\\Desktop\\WOR302_TEST\\beta.txt"
    assert calls[0]["content"] == content


def test_dsml_bar_variants_and_bare_json_body():
    ascii_variant = _dsml_invoke("bash", bar="|DSML|", command="echo ok")
    single_bar = _dsml_invoke("bash", bar="\uff5cDSML\uff5c", command="echo ok")
    bare_json = (
        f'<{_BAR} invoke name="bash">\n{{"command": "where adb"}}\n</{_BAR} invoke>'
    )
    assert _extract_tool_calls(ascii_variant, provider="deepseek") == [
        {"tool": "bash", "command": "echo ok"},
    ]
    assert _extract_tool_calls(single_bar, provider="deepseek") == [
        {"tool": "bash", "command": "echo ok"},
    ]
    assert _extract_tool_calls(bare_json, provider="deepseek") == [
        {"tool": "bash", "command": "where adb"},
    ]


def test_dsml_inline_attribute_arguments_with_powershell_values():
    # Live grammar variant: the model writes the argument fields inline in
    # the invoke token (JSON-fragment style), with escaped quotes and a
    # Windows redirection containing '>'.
    raw = (
        f"<{_BAR} calls>\n"
        f'<{_BAR} invoke name="bash", "command": "where adb & echo --- & '
        f'where java & dir \\"%LOCALAPPDATA%\\\\Android\\\\Sdk\\" 2>nul & '
        f'if exist \\"%LOCALAPPDATA%\\\\Android\\\\Sdk\\\\platform-tools\\\\adb.exe\\" '
        f'echo ADB_FOUND"\n'
        f"</{_BAR} invoke>\n</{_BAR} calls>"
    )
    calls = _extract_tool_calls(raw, provider="deepseek")
    assert calls == [{
        "tool": "bash",
        "command": (
            'where adb & echo --- & where java & dir "%LOCALAPPDATA%\\Android\\Sdk"'
            " 2>nul & if exist \"%LOCALAPPDATA%\\Android\\Sdk\\platform-tools"
            '\\adb.exe" echo ADB_FOUND'
        ),
    }]
    # Attribute style with a '>' inside a value is not truncated either.
    attr_style = (
        f'<{_BAR} invoke name="write_file" '
        f'path="C:\\Users\\vertu\\Desktop\\a.txt" content="hello > world">\n'
        f"</{_BAR} invoke>"
    )
    assert _extract_tool_calls(attr_style, provider="deepseek") == [{
        "tool": "write_file",
        "path": "C:\\Users\\vertu\\Desktop\\a.txt",
        "content": "hello > world",
    }]


def test_dsml_invoke_tool_call_alias_with_json_body_uses_real_tool_name():
    raw = (
        f"<{_BAR} calls>\n"
        f'<{_BAR} invoke name="tool_call">\n'
        '{"tool": "run_command", "command": "powershell -Command \\"Get-ChildItem\\""}\n'
        f"</{_BAR} invoke>\n</{_BAR} calls>"
    )
    calls = _extract_tool_calls(raw, provider="deepseek")
    assert calls == [{
        "tool": "run_command",
        "command": 'powershell -Command "Get-ChildItem"',
    }]


def test_malformed_dsml_never_silently_disappears_and_never_false_dispatches():
    malformed = f"<{_BAR} calls>\n<{_BAR} invoke name=\"bash\">\nunterminated..."
    calls = _extract_tool_calls(malformed, provider="deepseek")
    # The structural recovery never invents a call it cannot name; a
    # recoverable name without arguments is refused by schema validation
    # before any dispatch (see lifecycle test below).
    assert all(isinstance(call, dict) for call in calls)
    assert agent_module.has_tool_markup(malformed) is True
    assert calls[0]["tool"] == "bash"
    assert "command" not in calls[0]
    assert agent_module._parse_dsml_tool_calls("Le format DSML est interne.") == []
    assert agent_module.has_tool_markup("Le format DSML est interne.") is False


# ── 4, 13: streaming buffering and projection ──────────────────────────────

def test_chunked_dsml_is_buffered_never_leaked_and_parsed_once():
    agent = OSIAAgent.__new__(OSIAAgent)
    full = (
        "Je prepare le dossier.\n\n"
        + _dsml_response(_dsml_invoke("bash", command="adb devices"))
        + "Suite interdite"
    )
    _install_fake_deltas(agent, [full[i:i + 5] for i in range(0, len(full), 5)])
    emitted: list[str] = []
    content = agent._stream_with_sentences(lambda s: emitted.append(s))
    joined = " ".join(emitted)
    assert "DSML" not in joined and "\uff5c" not in joined
    assert "Suite interdite" not in joined
    assert "Je prepare le dossier." in joined
    assert content == full
    calls = _extract_tool_calls(content, provider="deepseek")
    assert calls == [{"tool": "bash", "command": "adb devices"}]


def test_native_fragmented_stream_remains_supported_by_non_streaming_path():
    # Native structured calls (OpenAI-compatible message.tool_calls) stay
    # structured and are normalized by the universal parser; they are never
    # converted into user-visible DSML text.
    native_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(
            name="write_file",
            arguments=json.dumps({
                "path": "C:\\x\\a.txt", "content": "hi",
            }),
        ),
    )
    message = {"tool_calls": [native_call]}
    calls = _extract_tool_calls("", message=message, provider="groq")
    assert calls == [{
        "tool": "write_file",
        "path": "C:\\x\\a.txt",
        "content": "hi",
    }]
    assert "DSML" not in json.dumps(calls)


def test_partial_xml_stream_is_buffered_not_spoken():
    agent = OSIAAgent.__new__(OSIAAgent)
    _install_fake_deltas(agent, [_tool_call("bash", command="ls")[:18]])
    emitted: list[str] = []
    agent._stream_with_sentences(lambda s: emitted.append(s))
    assert emitted == []


# ── 5, 8-11, 13-14: canonical lifecycle ────────────────────────────────────

def _patch_tool(monkeypatch, name, function):
    monkeypatch.setitem(TOOL_MAP, name, function)


def test_dsml_tool_call_dispatches_real_dispatcher(monkeypatch):
    dispatches: list[tuple] = []
    _patch_tool(
        monkeypatch, "create_directory",
        lambda path: dispatches.append(("create_directory", path))
        or f"Created directory: {path}",
    )
    agent = _agent(
        monkeypatch,
        [_dsml_response(_dsml_invoke(
            "create_directory", path="C:\\Users\\vertu\\Desktop\\WOR302_TEST",
        )), "Dossier cree."],
    )
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Cree le dossier WOR302_TEST.", history=[])
    assert dispatches == [
        ("create_directory", "C:\\Users\\vertu\\Desktop\\WOR302_TEST"),
    ]
    assert final == "Dossier cree."


def test_dsml_receipt_is_reinjected_and_provider_continues(monkeypatch):
    dispatches: list[str] = []
    _patch_tool(
        monkeypatch, "bash",
        lambda command: dispatches.append(command) or "adb: 1 device attached",
    )
    agent = _agent(
        monkeypatch,
        [
            _dsml_response(_dsml_invoke("bash", command="adb devices")),
            "Un appareil est connecte.",
        ],
    )
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Verifie adb.", history=[])
    completions = agent.client.chat.completions
    assert len(completions.calls) == 2
    second_messages = completions.calls[1]["messages"]
    assert any(
        "adb: 1 device attached" in str(message.get("content") or "")
        for message in second_messages
    )
    assert any(
        "<tool_call>" in str(message.get("content") or "")
        and '"bash"' in str(message.get("content") or "")
        for message in second_messages
    )
    assert dispatches == ["adb devices"]
    assert final == "Un appareil est connecte."


def test_finish_reason_stop_with_parsed_call_still_continues(monkeypatch):
    dispatched: list[str] = []
    _patch_tool(
        monkeypatch, "list_files",
        lambda path: dispatched.append(path) or "alpha.txt\nbeta.txt",
    )
    agent = _agent(
        monkeypatch,
        [
            _dsml_response(_dsml_invoke("list_files", path="C:\\Temp")),
            "Les deux fichiers sont presents.",
        ],
    )
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Liste C:\\Temp.", history=[])
    assert dispatched == ["C:\\Temp"]
    assert len(agent.client.chat.completions.calls) == 2
    assert final == "Les deux fichiers sont presents."


def test_mixed_xml_and_dsml_calls_all_dispatch_in_order(monkeypatch):
    order: list[str] = []
    _patch_tool(
        monkeypatch, "write_file",
        lambda path, content: order.append(path) or f"wrote {path}",
    )
    _patch_tool(
        monkeypatch, "list_files",
        lambda path: order.append(path) or "a.txt",
    )
    mixed = (
        _tool_call("write_file", path="C:/x/index.html", content="<b>hi</b>")
        + "\n"
        + _dsml_invoke(
            "write_file", path="C:/x/LISEZ-MOI.txt", content="doc",
        )
        + _dsml_invoke("list_files", path="C:/x")
    )
    agent = _agent(monkeypatch, [mixed, "Termine."])
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Cree les fichiers.", history=[])
    assert order == [
        "C:/x/index.html", "C:/x/LISEZ-MOI.txt", "C:/x",
    ]
    assert final == "Termine."


def test_malformed_dsml_round_never_dispatches_or_shows_protocol(monkeypatch):
    dispatched: list = []
    _patch_tool(
        monkeypatch, "bash",
        lambda command: dispatched.append(command) or "unexpected",
    )
    malformed = f"<{_BAR} calls>\n<{_BAR} invoke name=\"bash\">\nunterminated"
    agent = _agent(monkeypatch, [malformed, "Je n'ai pas pu executer la commande."])
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Verifie adb.", history=[])
    assert dispatched == []
    assert "DSML" not in final and "\uff5c" not in final


def test_raw_dsml_never_reaches_final_or_display_projection():
    raw = _dsml_response(_dsml_invoke("bash", command="adb devices"))
    assert agent_module.strip_raw_tool_tags(raw) == ""
    assert agent_module.strip_tool_artifacts(raw) == ""
    assert agent_module.clean_response(raw) == ""
    with_prose = "Voici l'etat actuel.\n" + raw
    assert agent_module.strip_tool_artifacts(with_prose).strip() == (
        "Voici l'etat actuel."
    )


# ── 7, 15-16: native protocol / provider contracts ─────────────────────────

def test_native_structured_call_stays_structured_and_dispatches(monkeypatch):
    dispatched: list[str] = []
    _patch_tool(
        monkeypatch, "create_directory",
        lambda path: dispatched.append(path) or "Created directory",
    )
    native_call = SimpleNamespace(
        id="call_native_1",
        type="function",
        function=SimpleNamespace(
            name="create_directory",
            arguments=json.dumps({"path": "C:\\Users\\vertu\\Desktop\\N"}),
        ),
    )
    agent = _agent(
        monkeypatch,
        [{"content": None, "tool_calls": [native_call]}, "Cree."],
        provider="groq",
        model="openai/gpt-oss-120b",
    )
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Cree le dossier N.", history=[])
    assert dispatched == ["C:\\Users\\vertu\\Desktop\\N"]
    assert final == "Cree."
    assert "DSML" not in final and "\uff5c" not in final
    assistant_records = [
        message for message in agent.messages
        if message.get("role") == "assistant" and "<tool_call>" in str(message.get("content"))
    ]
    assert assistant_records, "native call must be recorded as canonical structured call"


def _groq_tool_use_failed(failed_generation: str):
    error = RuntimeError("Error code: 400")
    error.body = {
        "error": {
            "code": "tool_use_failed",
            "type": "invalid_request_error",
            "message": "Tool choice is none, but model called a tool",
            "failed_generation": failed_generation,
        },
    }
    return error


def test_groq_failed_native_attempt_is_normalized_to_canonical_call():
    recover = agent_module._recover_groq_native_tool_attempt
    model = "openai/gpt-oss-120b"
    cases = {
        '{"name": "create_directory", "arguments": {"path": "C:\\\\Users\\\\x"}}':
            {"tool": "create_directory", "path": "C:\\Users\\x"},
        '{"name": "tool_call", "arguments": {"tool": "bash", "command": "adb devices"}}':
            {"tool": "bash", "command": "adb devices"},
        '{"name": "tool.run_command", "arguments": {"command": "where adb"}}':
            {"tool": "run_command", "command": "where adb"},
        '{"name": "tool_call", "arguments": {"type": "list_files", "path": "C:\\\\"}}':
            {"tool": "list_files", "path": "C:\\"},
        '{"name": "tool_call", "arguments": {"create_directory": '
        '{"path": "C:\\\\Users\\\\x"}}}':
            {"tool": "create_directory", "path": "C:\\Users\\x"},
    }
    for generation, expected in cases.items():
        message = recover("groq", model, _groq_tool_use_failed(generation))
        assert message is not None, generation
        calls = _extract_tool_calls("", message=message, provider="groq")
        assert calls == [expected], generation
    # Non-recoverable or out-of-scope routes leave the original error standing.
    assert recover("groq", model, _groq_tool_use_failed("not json")) is None
    assert recover(
        "groq", model, _groq_tool_use_failed('{"name": "unknown_tool"}'),
    ) is None
    assert recover("groq", model, _groq_tool_use_failed(
        '{"name": "container.exec", "arguments": {"cmd": ["ls"]}}',
    )) is None
    assert recover("groq", model, _groq_tool_use_failed(
        '{"name": "tool_call", "arguments": {"path": "C:\\\\x", '
        '"type": "directory"}}',
    )) is None
    assert recover("groq", "llama-3.3-70b-versatile", _groq_tool_use_failed(
        '{"name": "bash", "arguments": {"command": "x"}}',
    )) is None
    plain = RuntimeError("boom")
    plain.body = {"error": {"code": "invalid_request_error"}}
    assert recover("groq", model, plain) is None
    assert recover("groq", model, RuntimeError("no body")) is None


def test_groq_recovered_call_dispatches_and_turn_continues(monkeypatch):
    dispatched: list[tuple] = []
    _patch_tool(
        monkeypatch, "create_directory",
        lambda path: dispatched.append(("create_directory", path))
        or f"Created directory: {path}",
    )
    agent = _agent(
        monkeypatch,
        [
            _groq_tool_use_failed(
                '{"name": "create_directory", "arguments": '
                '{"path": "C:\\\\Users\\\\vertu\\\\Desktop\\\\WOR302_TEST"}}'
            ),
            "Dossier cree et verifie.",
        ],
        provider="groq",
        model="openai/gpt-oss-120b",
    )
    with patch("osia.action_policy.guard_tool_call", lambda *a, **k: None), \
            patch("osia.action_policy.is_final_external_tool_call", lambda *a, **k: False):
        final = agent.run("Cree le dossier WOR302_TEST.", history=[])
    completions = agent.client.chat.completions
    assert completions.calls, "provider must be invoked again after the receipt"
    assert "tools" not in completions.calls[0], "textual contract only on the wire"
    assert "tool_choice" not in completions.calls[0]
    assert dispatched == [
        ("create_directory", "C:\\Users\\vertu\\Desktop\\WOR302_TEST"),
    ]
    assert final == "Dossier cree et verifie."


# ── 12, 17-18: projection and Thinking/active-turn lifecycle ───────────────

def test_streamed_round_with_tool_call_only_speaks_legitimate_prose():
    agent = OSIAAgent.__new__(OSIAAgent)
    prose = "Je verifie l'environnement."
    mixed = (
        prose + "\n"
        + _dsml_response(_dsml_invoke("bash", command="where adb"))
    )
    _install_fake_deltas(agent, [mixed[i:i + 3] for i in range(0, len(mixed), 3)])
    emitted: list[str] = []
    agent._stream_with_sentences(lambda s: emitted.append(s))
    assert emitted == [prose]


def test_thinking_lifecycle_spans_tool_subtours_and_only_ends_terminal():
    from osia.gui.island_lifecycle import IslandLifecycle

    lifecycle = IslandLifecycle(lambda: None)
    turn_owner = "turn-wor302"
    assert lifecycle.begin("chat", turn_owner, "thinking") is True
    # Provider segment 1 emits a tool call; the canonical turn stays active.
    assert lifecycle.update("chat", turn_owner, "thinking") is True
    assert lifecycle.current_state() == "thinking"
    # Tool dispatch + result reinjection + provider segment 2.
    assert lifecycle.update("chat", turn_owner, "thinking") is True
    assert lifecycle.current_state() == "thinking"
    # A foreign owner can never steer this turn's state.
    assert lifecycle.update("chat", "turn-other", "idle", terminal=True) is False
    assert lifecycle.current_state() == "thinking"
    # Only the canonical terminal ends the state.
    assert lifecycle.update("chat", turn_owner, "idle", terminal=True) is True
    assert lifecycle.current_state() == "idle"
    assert lifecycle.update("chat", turn_owner, "thinking") is False


# ── 19-20: cancellation and outcome-unknown integrity ──────────────────────

def test_stop_cancels_exact_turn_only(monkeypatch, tmp_path):
    import osia.turn_terminality as terminality_module

    from osia.turn_terminality import TurnTerminalityController

    with patch.object(terminality_module, "CONFIG_DIR", str(tmp_path)):
        agent = OSIAAgent(lang="fr")
        controller = TurnTerminalityController(
            turn_id="turn-stop", objective="objective", execution_required=True,
        )
        agent._terminality_controller = controller
        assert agent.request_turn_cancellation(
            "turn-stop", effect_possible=False,
        ) is True
        assert controller.snapshot().get("terminal_state") == "user_cancelled"
        assert agent.turn_cancellation_committed("turn-stop") is True
        assert agent.turn_cancellation_committed("turn-other") is False


def test_outcome_unknown_prevents_blind_replay(monkeypatch):
    import time

    calls: list[str] = []

    def late_write(path, content):
        calls.append(path)
        time.sleep(0.05)
        return "late success"

    _patch_tool(monkeypatch, "write_file", late_write)
    original_timeout = agent_module._tool_timeout_seconds
    monkeypatch.setattr(
        agent_module,
        "_tool_timeout_seconds",
        lambda name, kwargs: (
            0.01 if name == "write_file"
            else original_timeout(name, kwargs)
        ),
    )
    record = agent_module._execute_tool_with_policy(
        "write_file", {"path": "C:/Temp/x.txt", "content": "x"}, lang="fr",
    )
    assert record.outcome == ExecutionOutcome.OUTCOME_UNKNOWN
    assert len(calls) == 1
    # The late completion is never retried or replayed by the executor.
    time.sleep(0.12)
    assert len(calls) == 1


# ── 21: preserved invariants ───────────────────────────────────────────────

def test_wor299_301_303_invariants_and_parse_before_strip_order(monkeypatch):
    source = (_ROOT / "osia" / "agent.py").read_text("utf-8", errors="ignore")
    assert "MAX_RESPONSE_TOKENS" not in source
    for forbidden in (
        "_MAX_NO_PROGRESS_CYCLES",
        "_record_useful_progress",
        "provider_cannot_progress_no_usable_path",
        "_MAX_IDENTICAL_ACTION_ATTEMPTS",
    ):
        assert forbidden not in source, forbidden
    # The universal parser keeps the protected priority contract and the
    # DSML format is part of it.
    assert source.index('("xml", lambda: _parse_xml_tool_calls') < source.index(
        '("dsml", lambda: _parse_dsml_tool_calls'
    )
    assert '("dsml"' in source
    assert "strip_raw_tool_tags" in source

    # Instrument the actual Groq normalization, universal parser and reasoning
    # stripper. The canonical call is deliberately inside <think>; it must be
    # structured before stripping removes the complete private block.
    from osia import groq_tool_text

    private = "PRIVATE_REASONING_SENTINEL"
    command = "echo parse-before-strip"
    raw = (
        "<think>" + private
        + "<tool_call><function=bash><parameter=command>"
        + command
        + "</parameter></function></tool_call></think>Visible answer."
    )
    agent = _agent(monkeypatch, [raw], provider="groq", model="fixture")
    events = []
    parsed_calls = []
    cleaned_visible_text = []
    normalize = groq_tool_text.normalize_groq_tool_text
    extract = agent_module._extract_tool_calls
    strip = agent_module.strip_reasoning_blocks

    def traced_normalize(text):
        events.append("normalize")
        return normalize(text)

    def traced_extract(text, message=None, provider=None):
        events.append("extract")
        calls = extract(text, message=message, provider=provider)
        parsed_calls.extend(calls)
        return calls

    def traced_strip(text):
        events.append("strip")
        cleaned = strip(text)
        cleaned_visible_text.append(agent_module.strip_raw_tool_tags(cleaned))
        return cleaned

    class StopAfterParse(Exception):
        pass

    def stop_before_dispatch(_agent, _calls):
        raise StopAfterParse

    monkeypatch.setattr(groq_tool_text, "normalize_groq_tool_text", traced_normalize)
    monkeypatch.setattr(agent_module, "_extract_tool_calls", traced_extract)
    monkeypatch.setattr(agent_module, "strip_reasoning_blocks", traced_strip)
    monkeypatch.setattr(
        agent_module, "_apply_unfinished_execution_decision", stop_before_dispatch,
    )
    with pytest.raises(StopAfterParse):
        agent.run("Run the fixture call.", history=[])

    assert events == ["normalize", "extract", "strip"]
    assert parsed_calls == [{"tool": "bash", "command": command}]
    assert cleaned_visible_text == ["Visible answer."]
    assert private not in cleaned_visible_text[0]
