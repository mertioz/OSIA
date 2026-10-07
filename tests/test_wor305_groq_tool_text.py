"""Exact captured Groq dialect through the existing canonical dispatcher."""
import json

import pytest

from osia import agent as runtime
from osia.groq_tool_text import normalize_groq_tool_text
from tests.test_wor305_phase3a_no_replay import dispatch
from wor305_test_support import scripted


def dialect(command):
    return '<tool_call>\n<function=bash>\n<parameter=command>' + command + '</parameter>\n</function>\n</tool_call>'


def extract(text):
    return runtime._extract_tool_calls(normalize_groq_tool_text(text), provider='groq')


@pytest.mark.parametrize('command', ['echo GROQ_FIXTURE_OK', '\n echo "C:\\Temp\\x"\n echo "<x> & amp; >"\n'])
@pytest.mark.parametrize('spacing', [False, True])
def test_values_and_whitespace(command, spacing):
    raw = dialect(command)
    if spacing:
        raw = raw.replace('<function=bash>', '<function = bash >').replace('<parameter=command>', '<parameter = command >')
    assert extract(raw) == [{'tool': 'bash', 'command': command}]


@pytest.mark.parametrize('raw', [
    dialect('echo safe').replace('</parameter>', ''),
    dialect('echo safe').replace('</function>', ''),
    dialect('echo safe').replace('</tool_call>', ''),
    dialect('echo safe').replace('</parameter>', '</parameter>junk'),
    dialect('echo safe').replace('</parameter>', '</parameter><parameter=command>echo other</parameter>'),
    dialect('{"tool":"bash","command":"echo unsafe"}').replace('</function>', ''),
])
def test_malformed_no_dispatch_and_recoverable(dispatch, monkeypatch, raw):
    assert extract(raw) == []
    a, requests = scripted(monkeypatch, [raw, 'The malformed call was not executed.'])
    a.provider = 'groq'
    invoked = []
    original = runtime._execute_tool_with_policy
    def observe(*args, **kwargs):
        invoked.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, '_execute_tool_with_policy', observe)
    final = a.run_terminal_turn('Execute the safe command.', turn_id='groq-invalid')
    assert not invoked
    assert len(requests) == 2
    assert '<function' not in final and '<tool_call' not in final
    assert 'NOTHING' in str(requests[-1]['messages'])


def test_exact_fixture_real_dispatch_and_receipt(dispatch, monkeypatch):
    command = '\necho GROQ_FIXTURE_OK\n'
    a, requests = scripted(monkeypatch, [dialect(command), 'Observed GROQ_FIXTURE_OK.'])
    a.provider = 'groq'
    records = []
    original = runtime._execute_tool_with_policy
    def observe(tool, arguments, **kwargs):
        result = original(tool, arguments, **kwargs)
        records.append((tool, arguments, result))
        return result
    monkeypatch.setattr(runtime, '_execute_tool_with_policy', observe)
    final = a.run_terminal_turn('Print GROQ_FIXTURE_OK.', turn_id='groq-fixture')
    assert len(records) == 1
    tool, args, result = records[0]
    assert (tool, args) == ('bash', {'command': command})
    assert result.dispatched and 'GROQ_FIXTURE_OK' in str(result.result)
    assert len(requests) == 2
    assert any('GROQ_FIXTURE_OK' in m.get('content', '') and m['role'] in {'tool', 'user'} for m in requests[-1]['messages'])
    assert final == 'Observed GROQ_FIXTURE_OK.'


def test_multiple_calls_and_existing_json_unchanged():
    assert len(extract(dialect('echo one') + dialect('echo two'))) == 2
    for raw in ['<tool_call>{"tool":"bash","command":"echo <function=bash>"}</tool_call>', '{"tool":"bash","command":"echo ok"}']:
        assert normalize_groq_tool_text(raw) == raw
        assert len(extract(raw)) == 1


def test_reasoning_without_tool_is_suppressed(dispatch, monkeypatch):
    a, requests = scripted(monkeypatch, ['<think>private reasoning</think>Answer.'])
    a.provider = 'groq'
    assert a.run_terminal_turn('Give an answer.', turn_id='reasoning') == 'Answer.'


def test_unclosed_reasoning_is_not_reinserted_into_retry_history(dispatch, monkeypatch):
    private = 'PRIVATE_REASONING_SENTINEL'
    a, requests = scripted(monkeypatch, ['<think>' + private, 'Answer.'])
    a.provider = 'groq'
    assert a.run_terminal_turn('Give an answer.', turn_id='unclosed-reasoning') == 'Answer.'
    assert private not in json.dumps(a.messages)
    assert private not in json.dumps(requests[-1]['messages'])
