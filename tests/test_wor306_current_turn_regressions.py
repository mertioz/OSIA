"""Focused regressions for current evidence and Computer Use schema repair."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from osia import action_policy, agent as agent_module, conversations, tool_attempts
from osia.agent import OSIAAgent, SYSTEM_PROMPT_CORE, get_system_prompt
from osia.computer_use_v4_integration import computer_use_v4_turn_scope
from osia.continuous_learning import LearningEngine, LearningStore
from osia.tool_attempts import ToolAttemptStore
from osia.turn_context import fixed_turn_context
from osia.tool_contracts import (
    computer_use_argument_shape,
    get_static_registry,
    persisted_computer_use_argument_diagnostic,
    tool_schema_fingerprint,
)


def test_prompt_stops_after_conclusive_current_evidence_without_weakening_truth():
    prompt = SYSTEM_PROMPT_CORE

    assert "Give the final answer when the full objective is established by evidence" in prompt
    assert "The selected provider/model remains the semantic authority through the turn." in prompt
    assert "OUTCOME_UNKNOWN is not success" in prompt
    assert "does not block a distinct fresh model-requested call" in prompt
    assert "Reuse fresh, authoritative receipts and observations" in prompt
    assert "Never claim an action or postcondition without supporting evidence." in prompt
    assert "Respect cancellation, target identity, and stale-state rejection." in prompt
    assert "resolve conflicts by observing further or stating uncertainty" in prompt
    assert "An unchanged screenshot proves neither failure nor success." in prompt
    assert "reconcile an uncertain effect before retrying" not in prompt


def test_argument_shape_keeps_only_structure_lengths_and_schema_enum_labels():
    spec = get_static_registry().get("computer_actions")
    private_text = "PRIVATE_TYPED_SEARCH_VALUE"
    shape = computer_use_argument_shape(
        {"actions": [{
                "kind": "type",
                "frame_id": "PRIVATE_FRAME_REFERENCE",
                "text": private_text,
                "private_field": "PRIVATE_UNKNOWN_VALUE",
            }]},
        spec.input_schema,
    )

    serialized = json.dumps(shape, ensure_ascii=False)
    assert "PRIVATE_TYPED_SEARCH_VALUE" not in serialized
    assert "PRIVATE_FRAME_REFERENCE" not in serialized
    assert "PRIVATE_UNKNOWN_VALUE" not in serialized
    item = shape["fields"]["actions"]["items"][0]["fields"]
    assert item["kind"] == {"type": "string", "length": 4, "schema_enum": "type"}
    assert item["text"] == {"type": "string", "length": len(private_text)}
    assert item["frame_id"] == {
        "type": "string", "length": len("PRIVATE_FRAME_REFERENCE"),
    }


def test_sanitized_diagnostic_round_trips_in_existing_conversation_metadata(
    tmp_path, monkeypatch,
):
    spec = get_static_registry().get("computer_actions")
    private_text = "PRIVATE_TYPED_SEARCH_VALUE"
    diagnostic = {
        "schema_version": 1,
        "conversation_id": "conversation-diagnostic",
        "request_id": "request-diagnostic",
        "turn_id": "turn-diagnostic",
        "provider_id": "deepseek",
        "model_id": "deepseek-flash",
        "first_call": {
            "tool_name": "computer_actions",
            "schema_sha256": tool_schema_fingerprint(spec.input_schema),
            "argument_shape": computer_use_argument_shape(
                {"actions": [{
                    "kind": "type",
                    "frame_id": "PRIVATE_FRAME_REFERENCE",
                    "text": private_text,
                }]},
                spec.input_schema,
            ),
            "validation": {
                "passed": False,
                "issues": [{
                    "path": "$.actions[0].text",
                    "code": "invalid_value",
                }],
            },
        },
        "repair": None,
        "dispatch_started": False,
    }
    safe = persisted_computer_use_argument_diagnostic(diagnostic)
    assert isinstance(safe, dict)
    monkeypatch.setattr(
        conversations, "CONVERSATIONS_DIR", tmp_path / "conversations",
    )
    conversation = conversations.create_conversation()
    receipt = conversations.commit_message(
        conversation["id"],
        "assistant",
        "ERROR: no action was executed",
        metadata={"_computer_use_argument_diagnostic_v1": safe},
    )
    saved = receipt["conversation"]["messages"][-1]
    serialized = json.dumps(saved, ensure_ascii=False)
    assert saved["_computer_use_argument_diagnostic_v1"] == safe
    assert private_text not in serialized
    assert "PRIVATE_FRAME_REFERENCE" not in serialized
    assert (
        saved["_computer_use_argument_diagnostic_v1"]["first_call"]
        ["argument_shape"]["fields"]["actions"]["items"][0]
        ["fields"]["kind"]["schema_enum"] == "type"
    )


class _ScriptedCompletions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.providers = []
        self.provider_getter = lambda: ""

    def create(self, **kwargs):
        if not self.outputs:
            pytest.fail("Unexpected provider invocation")
        self.calls.append(copy.deepcopy(kwargs))
        self.providers.append(self.provider_getter())
        content = self.outputs.pop(0)
        message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _tool_call(name, **arguments):
    return "<tool_call>" + json.dumps(
        {"tool": name, **arguments}, separators=(",", ":"),
    ) + "</tool_call>"


def test_partial_reply_after_tools_continues_the_same_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", ToolAttemptStore(tmp_path / "attempts"))
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", None)
    monkeypatch.setattr("osia.turn_terminality.CONFIG_DIR", str(tmp_path))
    first = tmp_path / "alpha.txt"
    second = tmp_path / "beta.txt"
    answer = "Both files have been written."
    completions = _ScriptedCompletions([
        _tool_call("write_file", path=str(first), content="alpha"),
        "Alpha has been written. Beta still needs to be created.",
        _tool_call("write_file", path=str(second), content="beta"),
        answer,
    ])
    agent = OSIAAgent(model="deepseek-flash", lang="en")
    agent.provider = "deepseek"
    agent._resolve_route = lambda _text: ("max", [])
    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    completions.provider_getter = lambda: agent.provider
    turn_id = "turn-finish-remaining-work"
    agent.configure_tool_attempts(
        conversation_id="conversation-finish-remaining-work",
        turn_id=turn_id,
        request_id="request-finish-remaining-work",
        final_response_message_id="message-finish-remaining-work",
    )
    spoken = []
    final = agent.run_terminal_turn(
        "Write alpha in alpha.txt and beta in beta.txt.",
        turn_id=turn_id, history=[], on_sentence=spoken.append,
        current_conversation_id="conversation-finish-remaining-work",
        turn_context=fixed_turn_context("en", source="fixture", reference=turn_id),
    )
    assert final == answer and spoken == [answer]
    assert first.read_text() == "alpha" and second.read_text() == "beta"
    assert completions.providers == ["deepseek"] * 4
    assert [request["model"] for request in completions.calls] == ["deepseek-flash"] * 4
    review_wire = json.dumps(completions.calls[2]["messages"], ensure_ascii=False)
    assert "The last assistant reply was a draft" in review_wire
    assert "Beta still needs to be created" in review_wire
    assert "Successfully wrote 5 bytes to" in review_wire
    assert "Write alpha in alpha.txt and beta in beta.txt." in review_wire
    assert agent.terminality_result(turn_id)["terminal_state"] == "success_verified"


def test_model_continuation_marker_leaves_the_objective_open(tmp_path):
    from osia.turn_terminality import TurnTerminalityController

    controller = TurnTerminalityController(
        turn_id="model-continues", objective="Finish both parts of the task.",
        execution_required=True, storage_dir=tmp_path,
    )
    controller.bind_outcome_contract()
    response = '<osia_completion>{"outcomes":[],"status":"continue"}</osia_completion>'
    assert controller.accept_completion(response) == ""
    assert controller.evaluate().action == "continue"
    assert controller.snapshot()["remaining_work"] == ["original_objective_satisfied"]
    assert not controller.final_reserved


@pytest.mark.parametrize("learning_enabled", [True, False])
def test_computer_use_repair_gets_current_schema_and_dispatches_once(
    tmp_path, monkeypatch, learning_enabled,
):
    dispatches = []
    tool_store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", tool_store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", None)
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "computer_actions",
        lambda actions: dispatches.append(copy.deepcopy(actions)) or {
            "ok": True,
            "receipt": {
                "dispatch_state": "effect_confirmed",
                "outcome": "confirmed_success",
            },
        },
    )
    monkeypatch.setattr(action_policy, "guard_tool_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        action_policy, "is_final_external_tool_call", lambda *_a, **_k: False,
    )
    monkeypatch.setattr(
        action_policy, "consume_pending_execution_permit",
        lambda *_a, **_k: True,
    )

    agent = OSIAAgent(model="deepseek-flash", lang="en")
    agent.provider = "deepseek"
    agent._resolve_route = lambda _text: ("max", [])
    learning_store = LearningStore(tmp_path / "learning" / "learning.json")
    if not learning_enabled:
        learning_store.set_enabled(False)
    agent._learning_engine = LearningEngine(learning_store, tool_store)
    completions = _ScriptedCompletions([
        _tool_call(
            "computer_actions",
            actions=[{
                "kind": "click",
                "x": 10,
                "y": 20,
                "prior_turn_frame": "SHOULD_NOT_BE_ECHOED_IN_DIAGNOSTIC",
            }],
        ),
        _tool_call(
            "computer_actions",
            actions=[
                {
                    "kind": "click", "frame_id": "frame_current",
                    "x": 10, "y": 20, "effect_id": "focus search",
                },
                {
                    "kind": "type", "frame_id": "frame_current",
                    "text": "Subnautica", "effect_id": "enter query",
                },
                {
                    "kind": "keypress", "frame_id": "frame_current",
                    "key": "enter", "effect_id": "submit query",
                    "observe_after": True,
                },
            ],
        ),
        "The synthetic tool returned; this fixture does not prove the game page opened.",
    ])
    completions.provider_getter = lambda: agent.provider
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    agent.configure_tool_attempts(
        conversation_id="conversation-current-turn",
        turn_id="turn-current-turn",
        request_id="request-current-turn",
        final_response_message_id="message-final-current-turn",
        owner_scope="chat",
    )

    final = agent.run(
        "Use the current desktop frame to search for Subnautica.",
        history=[],
        current_conversation_id="conversation-current-turn",
    )

    schema = get_static_registry().get("computer_actions").input_schema
    schema_text = json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    repair_messages = completions.calls[1]["messages"]
    repair_wire = "\n".join(
        str(item.get("content") or "") for item in repair_messages
    )
    assert "Current tool JSON Schema: " + schema_text in repair_wire
    assert "$.actions[0].frame_id" in repair_wire
    repair_result = next(
        str(item.get("content") or "")
        for item in repair_messages
        if "Current tool JSON Schema:" in str(item.get("content") or "")
    )
    assert "SHOULD_NOT_BE_ECHOED_IN_DIAGNOSTIC" not in repair_result
    assert [call["model"] for call in completions.calls] == [
        "deepseek-flash", "deepseek-flash", "deepseek-flash",
    ]
    assert completions.providers == ["deepseek", "deepseek", "deepseek"]
    assert len(dispatches) == 1
    assert dispatches[0][0]["frame_id"] == "frame_current"
    assert dispatches[0][1]["text"] == "Subnautica"
    assert "does not prove" in final
    diagnostic = agent.last_tool_argument_diagnostic
    assert diagnostic["first_call"]["tool_name"] == "computer_actions"
    assert diagnostic["first_call"]["schema_sha256"] == tool_schema_fingerprint(
        schema,
    )
    assert diagnostic["first_call"]["validation"]["passed"] is False
    assert any(
        issue["path"] == "$.actions[0].frame_id"
        for issue in diagnostic["first_call"]["validation"]["issues"]
    )
    assert diagnostic["repair"]["validation"]["passed"] is True
    # The test dispatcher was called once, but the stub intentionally creates
    # no physical ToolAttempt/ActionTruth dispatch.
    assert diagnostic["dispatch_started"] is False
    persisted = persisted_computer_use_argument_diagnostic(diagnostic)
    assert isinstance(persisted, dict)
    serialized_diagnostic = json.dumps(persisted, ensure_ascii=False)
    assert "Subnautica" not in serialized_diagnostic
    assert "SHOULD_NOT_BE_ECHOED_IN_DIAGNOSTIC" not in serialized_diagnostic
    assert "frame_current" not in serialized_diagnostic


def test_current_v4_prompt_contains_only_this_turn_capability_inventory():
    with computer_use_v4_turn_scope(
        "deepseek", "deepseek-flash",
        conversation_id="conversation-schema-audit",
        turn_id="turn-schema-audit",
    ):
        prompt = get_system_prompt("fr", "deepseek-flash", "deepseek")

    assert "- computer_actions(actions:list):" in prompt
    assert "- computer_observation(frame_id:string" in prompt
    assert "emit top-level JSON inside `<tool_call>` tags" in prompt
    assert "Never claim an action or postcondition without supporting evidence." in prompt
    actions_schema = get_static_registry().get("computer_actions").input_schema
    item_schema = actions_schema["properties"]["actions"]["items"]
    assert item_schema["required"] == ["kind", "frame_id"]
    assert actions_schema["properties"]["actions"]["maxItems"] == 3


@pytest.mark.parametrize("learning_enabled", [True, False])
@pytest.mark.parametrize("lang", ["en", "fr"])
def test_invalid_second_computer_call_stays_non_dispatched_and_truthful(
    tmp_path, monkeypatch, learning_enabled, lang,
):
    dispatches = []
    tool_store = ToolAttemptStore(tmp_path / "attempts")
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", tool_store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", None)
    monkeypatch.setitem(
        agent_module.TOOL_MAP,
        "computer_observation",
        lambda **kwargs: dispatches.append(kwargs) or "unexpected dispatch",
    )
    agent = OSIAAgent(model="deepseek-flash", lang=lang)
    agent.provider = "deepseek"
    agent._resolve_route = lambda _text: ("max", [])
    learning_store = LearningStore(tmp_path / "learning" / "learning.json")
    if not learning_enabled:
        learning_store.set_enabled(False)
    agent._learning_engine = LearningEngine(learning_store, tool_store)
    completions = _ScriptedCompletions([
        _tool_call(
            "computer_observation",
            status="satisfied",
            observed_state="Steam shows Running and Stop.",
        ),
        _tool_call(
            "computer_observation",
            frame_id="frame_current",
            status="done",
            observed_state="Steam shows Running and Stop.",
        ),
        (
            "L'écran n'a pas été observé : aucun des appels invalides n'a été envoyé."
            if lang == "fr" else
            "The screen was not observed: neither invalid call was dispatched."
        ),
    ])
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    agent.configure_tool_attempts(
        conversation_id="conversation-invalid-repair",
        turn_id="turn-invalid-repair",
        request_id="request-invalid-repair",
        final_response_message_id="message-final-invalid-repair",
        owner_scope="chat",
    )

    with patch("osia.turn_terminality.CONFIG_DIR", str(tmp_path)):
        final = agent.run_terminal_turn(
            (
                "Observe l'écran de bureau actuel."
                if lang == "fr" else
                "Observe the current synthetic desktop frame."
            ),
            turn_id="turn-invalid-repair",
            history=[],
            current_conversation_id="conversation-invalid-repair",
            turn_context=fixed_turn_context(
                lang, source="fixture", reference="turn-invalid-repair",
            ),
        )

    assert len(completions.calls) == 3
    assert dispatches == []
    assert final == (
        "L'écran n'a pas été observé : aucun des appels invalides n'a été envoyé."
        if lang == "fr" else
        "The screen was not observed: neither invalid call was dispatched."
    )
    assert all(call["model"] == "deepseek-flash" for call in completions.calls)
    third_round = "\n".join(
        str(item.get("content") or "")
        for item in completions.calls[2]["messages"]
    )
    # WOR-341 keeps each rejection inside the selected model's tool loop,
    # rather than ending a segment after the second proposal.
    assert "This call was not dispatched and had no effect." in third_round
    assert '"dispatch_state":"not_dispatched"' in third_round
    assert "Current tool JSON Schema:" in third_round
    assert "$.status" in third_round
    result = agent.terminality_result("turn-invalid-repair") or {}
    assert result["terminal_state"] == "success_verified"
    assert result["reason_code"] == "selected_model_completed"
    diagnostic = agent.last_tool_argument_diagnostic
    assert diagnostic["first_call"]["tool_name"] == "computer_observation"
    assert diagnostic["first_call"]["validation"]["issues"] == [{
        "path": "$.frame_id",
        "code": "missing_required_field",
    }]
    assert diagnostic["repair"]["validation"]["passed"] is False
    assert diagnostic["repair"]["validation"]["issues"] == [{
        "path": "$.status",
            "code": "enum",
    }]
    assert diagnostic["dispatch_started"] is False
    persisted = persisted_computer_use_argument_diagnostic(diagnostic)
    assert isinstance(persisted, dict)
    serialized_diagnostic = json.dumps(persisted, ensure_ascii=False)
    assert "Steam shows Running" not in serialized_diagnostic
    assert "frame_current" not in serialized_diagnostic
    repair_wire = "\n".join(
        str(item.get("content") or "")
        for item in completions.calls[1]["messages"]
    )
    assert "$.frame_id" in repair_wire
    schema_text = json.dumps(
        get_static_registry().get("computer_observation").input_schema,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert "Current tool JSON Schema: " + schema_text in repair_wire
    assert "$.status" in third_round


def test_new_turn_binding_clears_prior_execution_state():
    agent = OSIAAgent(model="fixture-model", lang="en")
    previous_controller = SimpleNamespace(turn_id="previous-turn")
    agent._unfinished_execution = {"turn_id": "previous-turn"}
    agent._terminality_controller = previous_controller
    agent._terminality_strategy = object()
    agent._resume_unknown_effect = True
    agent._continuation_resolution_pending = True
    agent._continuation_resume_selected = True
    agent._continuation_reconcile_required = True
    agent._continuation_reconciled = True
    agent._continuation_state_refreshed = True
    agent._continuation_control_retry_used = True

    agent._bind_canonical_turn_execution_state("current-turn")

    assert agent._unfinished_execution is None
    assert agent._terminality_controller is None
    assert agent._terminality_strategy is None
    assert agent._resume_unknown_effect is False
    assert agent._continuation_resolution_pending is False
    assert agent._continuation_resume_selected is False
    assert agent._continuation_reconcile_required is False
    assert agent._continuation_reconciled is False
    assert agent._continuation_state_refreshed is False
    assert agent._continuation_control_retry_used is False


def test_fresh_run_clears_previous_argument_diagnostic():
    agent = OSIAAgent(model="deepseek-flash", lang="en")
    agent.provider = "deepseek"
    agent._resolve_route = lambda _text: ("max", [])
    agent.last_tool_argument_diagnostic = {
        "schema_version": 1,
        "turn_id": "previous-turn",
    }
    completions = _ScriptedCompletions(["A fresh answer."])
    agent.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )

    final = agent.run(
        "Answer the current informational request.",
        history=[],
        current_conversation_id="conversation-fresh-diagnostic",
    )

    assert final == "A fresh answer."
    assert agent.last_tool_argument_diagnostic is None
