import json
import types

import pytest

from osia import agent as agent_module
from osia.agent import OSIAAgent
from osia.tool_attempts import WAITING_TOOL_RESULT
from osia.tool_contracts import (
    ToolValidationError,
    get_static_registry,
    validate_arguments,
    validate_tool_arguments,
    validation_error_details,
)


_STATE_REF = {
    "schema_version": 1,
    "browser_session_id": "browser_AAAAAAAAAAAAAAAAAAAAAAAA",
    "context_id": "context_AAAAAAAAAAAAAAAAAAAAAAAA",
    "context_epoch": 1,
    "page_id": "page_AAAAAAAAAAAAAAAAAAAAAAAA",
    "page_generation": 1,
    "navigation_id": "nav_AAAAAAAAAAAAAAAAAAAAAAAA",
    "document_epoch": 1,
    "created_at": 1.0,
}
_ELEMENT_REF = "el_AAAAAAAAAAAAAAAAAAAAAAAA"


def _install_rounds(agent, outputs, observations=None):
    remaining = iter(outputs)

    def fake_stream(self, callback, *, on_first_token=None, guard=None):
        if observations is not None:
            observations.append({
                "provider": self.provider,
                "model": self.model,
                "messages": json.dumps(self.messages, ensure_ascii=False),
            })
        return next(remaining)

    agent._stream_with_sentences = types.MethodType(fake_stream, agent)


def test_validation_diagnostics_are_safe_field_and_type_only():
    schema = get_static_registry().get("open_browser").input_schema
    private_value = "PRIVATE_REJECTED_BROWSER_VALUE"
    with pytest.raises(ToolValidationError) as raised:
        validate_arguments(schema, {"name": private_value})
    details = validation_error_details(raised.value)
    encoded = json.dumps(details, sort_keys=True)
    assert details["status"] == "failed_before_effect"
    assert details["dispatch_state"] == "not_dispatched"
    assert private_value not in encoded
    assert any(issue["path"] == "$.name" for issue in details["issues"])
    assert any("allowed" in issue.get("expected", {}) for issue in details["issues"])


def test_one_repair_dispatches_once_with_exact_provider_and_model(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: dispatches.append("called") or "synthetic system info",
    )
    agent = OSIAAgent(model="repair-model", lang="en")
    agent.provider = "repair-provider"
    observations = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"get_system_info","private_field":"SECRET"}</tool_call>',
        '<tool_call>{"tool":"get_system_info"}</tool_call>',
        "The repaired read completed.",
    ], observations)
    final = agent.run("Read synthetic system info.", history=[], on_sentence=lambda _x: None)
    assert final == "The repaired read completed."
    assert dispatches == ["called"]
    assert len(observations) == 3
    assert {(item["provider"], item["model"]) for item in observations} == {
        ("repair-provider", "repair-model")
    }
    repair_feedback = observations[1]["messages"].rsplit("<tool_result>", 1)[-1]
    assert "SECRET" not in repair_feedback
    assert "$.private_field" in repair_feedback
    assert "failed_before_effect" in observations[1]["messages"]
    assert final != WAITING_TOOL_RESULT


def test_second_invalid_call_stops_without_dispatch_or_waiting(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: dispatches.append("called") or "unexpected",
    )
    agent = OSIAAgent(model="repair-model", lang="en")
    agent.provider = "repair-provider"
    observations = []
    _install_rounds(agent, [
        '<tool_call>{"tool":"get_system_info","extra":"FIRST_SECRET"}</tool_call>',
        '<tool_call>{"tool":"get_system_info","extra":"SECOND_SECRET"}</tool_call>',
    ], observations)
    final = agent.run("Read synthetic system info.", history=[], on_sentence=lambda _x: None)
    assert dispatches == []
    assert len(observations) == 2
    assert "single repair attempt" in final
    assert final != WAITING_TOOL_RESULT
    exported = json.dumps(agent.exported_history(), ensure_ascii=False)
    repair_feedback = observations[1]["messages"].rsplit("<tool_result>", 1)[-1]
    assert "FIRST_SECRET" not in repair_feedback
    assert "SECOND_SECRET" not in final
    assert "SECOND_SECRET" not in exported


def test_missing_replacement_tool_call_returns_precise_zero_dispatch(monkeypatch):
    dispatches = []
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "get_system_info",
        lambda: dispatches.append("called") or "unexpected",
    )
    agent = OSIAAgent(model="repair-model", lang="en")
    agent.provider = "repair-provider"
    _install_rounds(agent, [
        '<tool_call>{"tool":"get_system_info","extra":"SECRET"}</tool_call>',
        "I will try something else.",
    ])
    final = agent.run("Read synthetic system info.", history=[], on_sentence=lambda _x: None)
    assert dispatches == []
    assert "single repair attempt" in final
    assert final != WAITING_TOOL_RESULT


def test_postcondition_activation_requires_wait_spec_before_dispatch():
    spec = get_static_registry().get("activate_browser_element")
    with pytest.raises(ToolValidationError) as raised:
        validate_tool_arguments(spec, {
            "state_ref": _STATE_REF,
            "element_ref": _ELEMENT_REF,
        })
    details = validation_error_details(raised.value)
    assert details["dispatch_state"] == "not_dispatched"
    assert details["issues"] == [{
        "path": "$.wait_spec",
        "code": "missing_required_field",
        "expected": "object",
    }]


def test_wait_spec_repair_dispatches_once_with_exact_route(monkeypatch):
    dispatches = []

    def activate_once(state_ref, element_ref, wait_spec=None, **_kwargs):
        dispatches.append((state_ref, element_ref, wait_spec))
        return {
            "ok": True,
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
            }
        }

    monkeypatch.setitem(
        agent_module.TOOL_MAP, "activate_browser_element", activate_once,
    )
    monkeypatch.setattr("osia.action_policy.guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "osia.action_policy.is_final_external_tool_call", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        "osia.action_policy.consume_pending_execution_permit",
        lambda *_a, **_k: True,
    )
    agent = OSIAAgent(model="repair-model", lang="en")
    agent.provider = "repair-provider"
    observations = []
    state_json = json.dumps(_STATE_REF, separators=(",", ":"))
    _install_rounds(agent, [
        '<tool_call>{"tool":"activate_browser_element",'
        f'"state_ref":{state_json},"element_ref":"{_ELEMENT_REF}"}}'
        '</tool_call>',
        '<tool_call>{"tool":"activate_browser_element",'
        f'"state_ref":{state_json},"element_ref":"{_ELEMENT_REF}",'
        '"wait_spec":{"condition_type":"text_appearance",'
        '"expected":"ready","timeout_seconds":80}}</tool_call>',
        "The repaired activation completed once.",
    ], observations)

    final = agent.run(
        "Activate the controlled element once.",
        history=[],
        on_sentence=lambda _x: None,
    )

    assert final == "The repaired activation completed once."
    assert len(dispatches) == 1
    assert dispatches[0][2]["timeout_seconds"] == 80
    assert {(item["provider"], item["model"]) for item in observations} == {
        ("repair-provider", "repair-model")
    }
    repair_feedback = observations[1]["messages"].rsplit("<tool_result>", 1)[-1]
    assert "$.wait_spec" in repair_feedback
    assert '\\"expected\\":\\"object\\"' in repair_feedback


def test_second_invalid_wait_spec_repair_stops_with_zero_dispatch(monkeypatch):
    dispatches = []
    private_value = "PRIVATE_REJECTED_WAIT_VALUE"
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "activate_browser_element",
        lambda **_kwargs: dispatches.append("called") or "unexpected",
    )
    agent = OSIAAgent(model="repair-model", lang="en")
    agent.provider = "repair-provider"
    observations = []
    state_json = json.dumps(_STATE_REF, separators=(",", ":"))
    _install_rounds(agent, [
        '<tool_call>{"tool":"activate_browser_element",'
        f'"state_ref":{state_json},"element_ref":"{_ELEMENT_REF}"}}'
        '</tool_call>',
        '<tool_call>{"tool":"activate_browser_element",'
        f'"state_ref":{state_json},"element_ref":"{_ELEMENT_REF}",'
        '"wait_spec":{"condition_type":"text_appearance",'
        f'"expected":"{private_value}"}}</tool_call>',
    ], observations)

    final = agent.run(
        "Activate the controlled element once.",
        history=[],
        on_sentence=lambda _x: None,
    )

    assert dispatches == []
    assert "single repair attempt" in final
    assert private_value not in final
    assert private_value not in json.dumps(agent.exported_history())
