"""WOR-262 structured-planning regressions from the physical Calculator run."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from osia.agent import OSIAAgent
from osia.autonomous import diagnostics, planner
from osia.autonomous.orchestrator import Orchestrator
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    TurnResult,
)
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.execution_strategy import build_execution_strategy, tool_preflight


TASK = "Open Calculator and calculate 347 × 29. Verify the result is 10063."

# The physical account-provider final messages were valid raw JSON. Incidental
# wording is sanitized while preserving their exact phase/task cardinality.
PHYSICAL_INITIAL_PROVIDER_FINAL = json.dumps({
    "phases": [
        {
            "name": "Open Calculator",
            "tasks": [{
                "objective": "Open Calculator.",
                "success_criterion": "A Calculator window is visible.",
            }],
        },
        {
            "name": "Calculate and verify",
            "tasks": [
                {
                    "objective": "Calculate 347 × 29 in Calculator.",
                    "success_criterion": "Calculator visibly shows 10063.",
                },
                {
                    "objective": "Verify the displayed result.",
                    "success_criterion": "The exact displayed result is 10063.",
                },
            ],
        },
    ],
}, separators=(",", ":"), ensure_ascii=False)

PHYSICAL_CORRECTIVE_PROVIDER_FINAL = json.dumps({
    "phases": [{
        "name": "Calculate and verify",
        "tasks": [
            {
                "objective": "Open Calculator and enter 347 × 29.",
                "success_criterion": "Calculator displays the expression.",
            },
            {
                "objective": "Evaluate the expression.",
                "success_criterion": "Calculator displays 10063.",
            },
            {
                "objective": "Verify the exact result.",
                "success_criterion": "The visible value is exactly 10063.",
            },
        ],
    }],
}, separators=(",", ":"), ensure_ascii=False)

# What the legacy normal action loop returned to planner._attempt after seeing
# those provider finals: one-line prose, not the designated final content part.
# Length and start/end shape match the two physical boundary returns.
PHYSICAL_INITIAL_LEGACY_RETURN = "I" + ("x" * 119) + "."
PHYSICAL_CORRECTIVE_LEGACY_RETURN = "I" + ("x" * 81) + "."

VALID = (
    '{"phases":[{"name":"Calculate","tasks":['
    '{"objective":"Open Calculator and calculate 347 × 29.",'
    '"success_criterion":"Calculator visibly shows 10063."}]}]}'
)
INVALID_SCHEMA = '{"phases":[{"name":"Calculate","tasks":[{"goal":"x"}]}]}'
TRUNCATED = '{"phases":[{"name":"Calculate","tasks":['


class _LegacyAgent:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def run(self, prompt, **_kwargs):
        self.calls.append(prompt)
        return self.outputs.pop(0) if self.outputs else ""


class _StructuredAgent(_LegacyAgent):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.schemas = []

    def run(self, *_args, **_kwargs):  # pragma: no cover - must never be used
        raise AssertionError("the normal action loop is not a planning boundary")

    def run_planning(self, prompt, *, output_schema, **_kwargs):
        self.calls.append(prompt)
        self.schemas.append(output_schema)
        return self.outputs.pop(0) if self.outputs else ""


def _plan(agent, failures=None):
    return planner.plan_objective(
        TASK,
        agent,
        language="en",
        run_id="49c61a9d29d64c90a5dc732f7ec899b8",
        failure_cb=failures.append if failures is not None else None,
    )


def test_physical_initial_legacy_boundary_reproduces_invalid_json():
    failures = []
    result = _plan(_LegacyAgent([
        PHYSICAL_INITIAL_LEGACY_RETURN,
        PHYSICAL_INITIAL_LEGACY_RETURN,
    ]), failures)
    assert result is None
    assert failures[-1].reason == "invalid_json"
    assert failures[-1].output_category == planner.MALFORMED_JSON
    assert len(PHYSICAL_INITIAL_LEGACY_RETURN) == 121


def test_physical_corrective_legacy_boundary_reproduces_second_invalid_json():
    failures = []
    agent = _LegacyAgent([
        PHYSICAL_INITIAL_LEGACY_RETURN,
        PHYSICAL_CORRECTIVE_LEGACY_RETURN,
    ])
    result = _plan(agent, failures)
    assert result is None
    assert len(agent.calls) == 2
    assert failures[-1].reason == "invalid_json"
    assert failures[-1].output_category == planner.MALFORMED_JSON
    assert len(PHYSICAL_CORRECTIVE_LEGACY_RETURN) == 83


def test_corrected_boundary_accepts_both_physical_provider_final_shapes():
    for raw, phase_count in (
        (PHYSICAL_INITIAL_PROVIDER_FINAL, 2),
        (PHYSICAL_CORRECTIVE_PROVIDER_FINAL, 1),
    ):
        agent = _StructuredAgent([raw])
        phases = _plan(agent)
        assert len(phases) == phase_count
        assert len(agent.calls) == 1
        assert agent.schemas[0]["required"] == ["phases"]


def test_valid_raw_json_is_accepted_unchanged_at_decode_boundary():
    decoded = planner._decode_planning_value(VALID)
    assert decoded.category == ""
    assert json.dumps(decoded.value, ensure_ascii=False, separators=(",", ":")) == VALID


def test_json_fence_is_rejected_because_physical_root_did_not_require_it():
    decoded = planner._decode_planning_value(f"```json\n{VALID}\n```")
    assert decoded.value is None
    assert decoded.category == planner.WRAPPED_JSON


def test_prose_plus_json_without_provider_boundary_fails_closed():
    decoded = planner._decode_planning_value("Here is the plan: " + VALID)
    assert decoded.value is None
    assert decoded.category == planner.MALFORMED_JSON


def test_two_competing_json_objects_fail_closed():
    decoded = planner._decode_planning_value(VALID + "\n" + VALID)
    assert decoded.value is None
    assert decoded.category == planner.MULTIPLE_PAYLOADS


def test_truncated_json_has_precise_decoder_diagnostic():
    decoded = planner._decode_planning_value(TRUNCATED)
    assert decoded.value is None
    assert decoded.category == planner.TRUNCATED_JSON
    assert decoded.error_type == "JSONDecodeError"
    assert isinstance(decoded.error_position, int)


def test_valid_json_invalid_schema_is_not_reported_as_invalid_json():
    failures = []
    assert _plan(_StructuredAgent([INVALID_SCHEMA, INVALID_SCHEMA]), failures) is None
    assert failures[-1].reason == "invalid_schema"
    assert failures[-1].output_category == planner.VALID_JSON_INVALID_SCHEMA
    assert failures[-1].schema_error == "canonical_schema_mismatch"


def test_missing_required_objective_field_is_schema_failure():
    raw = (
        '{"phases":[{"name":"P","tasks":['
        '{"success_criterion":"visible"}]}]}'
    )
    failures = []
    assert _plan(_StructuredAgent([raw, raw]), failures) is None
    assert failures[-1].output_category == planner.VALID_JSON_INVALID_SCHEMA


def test_unknown_extra_fields_follow_existing_drop_on_normalize_contract():
    raw = (
        '{"phases":[{"name":"P","unknown_phase":true,"tasks":['
        '{"objective":"Do it","success_criterion":"It is visible",'
        '"unknown_task":17}]}],"unknown_root":"kept only at provider boundary"}'
    )
    phases = _plan(_StructuredAgent([raw]))
    assert phases[0]["name"] == "P"
    assert "unknown_phase" not in phases[0]
    assert "unknown_task" not in phases[0]["tasks"][0]
    schema = planner._planning_output_schema()
    assert schema["additionalProperties"] is False


def test_corrective_retry_receives_only_precise_failure_category_and_schema():
    agent = _StructuredAgent([TRUNCATED, VALID])
    phases = _plan(agent)
    assert phases
    assert len(agent.calls) == 2
    assert "Previous output category: TRUNCATED_JSON." in agent.calls[1]
    assert "ONLY raw JSON" in agent.calls[1]
    assert PHYSICAL_INITIAL_LEGACY_RETURN not in agent.calls[1]


def test_first_valid_attempt_uses_zero_corrective_calls():
    agent = _StructuredAgent([VALID])
    assert _plan(agent)
    assert len(agent.calls) == 1


def test_first_invalid_second_valid_uses_exactly_one_corrective_call():
    agent = _StructuredAgent([TRUNCATED, VALID])
    assert _plan(agent)
    assert len(agent.calls) == 2


def test_first_invalid_second_invalid_has_one_precise_final_callback():
    failures = []
    agent = _StructuredAgent([TRUNCATED, "not json"])
    assert _plan(agent, failures) is None
    assert len(agent.calls) == 2
    assert len(failures) == 1
    assert failures[0].output_category == planner.MALFORMED_JSON


def test_precise_planning_category_survives_sanitized_diagnostic_projection():
    diagnostic = diagnostics.make_diagnostic(
        None,
        stage=diagnostics.AutonomousStage.PLANNING,
        category=diagnostics.FailureCategory.INVALID_PLANNING_OUTPUT,
        reason_code="invalid_json",
        planning_output_category=planner.TRUNCATED_JSON,
    )
    assert diagnostic["planning_output_category"] == planner.TRUNCATED_JSON
    assert diagnostics.normalize_diagnostic(diagnostic) == diagnostic


def test_no_planning_payload_fails_closed():
    failures = []
    assert _plan(_StructuredAgent(["", ""]), failures) is None
    assert failures[-1].output_category == planner.NO_PLANNING_PAYLOAD


class _CaptureAppClient:
    generation = 1

    def __init__(self, text=VALID):
        self.text = text
        self.starts = []
        self.turns = []

    def start_or_resume_conversation(self, **kwargs):
        self.starts.append(kwargs)
        return "thread-planning"

    def run_turn(self, **kwargs):
        self.turns.append(kwargs)
        return TurnResult(
            text=self.text,
            thread_id="thread-planning",
            turn_id="turn-planning",
            status="completed",
            output_started=True,
        )


def test_account_provider_uses_output_schema_and_empty_dynamic_tool_surface():
    client = _CaptureAppClient()
    provider = ChatGPTAccountProvider(client)
    schema = planner._planning_output_schema()
    result = provider.complete(
        conversation_id="wor262-planning",
        model="gpt-5.6-terra",
        messages=[{"role": "user", "content": "plan only"}],
        output_schema=schema,
        planning_only=True,
    )
    assert result.text == VALID
    assert client.starts[0]["dynamic_tools"] == ()
    assert "planning-only" in client.starts[0]["base_instructions"]
    assert client.turns[0]["output_schema"] == schema


def test_app_server_selects_only_agent_message_with_reasoning_and_tool_event():
    client = AppServerClient()
    captured = []

    def fake_request(method, params, **_kwargs):
        captured.append((method, params))
        if method != "turn/start":
            return {}
        thread_id = params["threadId"]
        client._dispatch_notification(
            "item/reasoning/textDelta",
            {"threadId": thread_id, "delta": "private reasoning fixture"},
        )
        client._dispatch_notification(
            "item/started",
            {
                "threadId": thread_id,
                "item": {"id": "web-1", "type": "webSearch"},
            },
        )
        client._dispatch_notification(
            "item/completed",
            {
                "threadId": thread_id,
                "item": {"id": "web-1", "type": "webSearch"},
            },
        )
        client._dispatch_notification(
            "item/agentMessage/delta",
            {"threadId": thread_id, "delta": VALID},
        )
        client._dispatch_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        return {"turn": {"id": "turn-1"}}

    client._request = fake_request
    schema = planner._planning_output_schema()
    result = client.run_turn(
        thread_id="thread-1",
        model="gpt-5.6-terra",
        text="plan only",
        output_schema=schema,
    )
    assert result.text == VALID
    assert len(result.capability_events) == 2
    turn_params = next(params for method, params in captured if method == "turn/start")
    assert turn_params["outputSchema"] == schema
    assert "private reasoning fixture" not in result.text


class _CompletionClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self.outputs.pop(0), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def consume_capability_events(self):
        return ()


def test_real_agent_planning_surface_never_enters_action_dispatch():
    agent = OSIAAgent(model="planning-fixture", lang="en")
    client = _CompletionClient([VALID])
    agent.client = client
    agent.provider = "chatgpt_account"
    agent.model = "gpt-5.6-terra"
    with patch(
        "osia.agent._execute_tool_with_policy",
        side_effect=AssertionError("planning dispatched an OSIA tool"),
    ) as dispatch:
        phases = _plan(agent)
    assert phases
    assert dispatch.call_count == 0
    assert len(client.calls) == 1
    assert client.calls[0]["planning_only"] is True
    assert client.calls[0]["output_schema"]["required"] == ["phases"]


def _physical_wrapper(objective=TASK, criterion="Calculator visibly shows 10063."):
    task = {
        "id": "0.0",
        "objective": objective,
        "success_criterion": criterion,
        "requirement_id": "",
        "semantic_next_strategy": "",
    }
    orchestrator = Orchestrator(Mock())
    orchestrator._state = {
        "objective": TASK,
        "phases": [{"name": "Calculate and verify", "tasks": [task]}],
        "browser_checkpoint": {},
        "checklist": [],
        "document_sources": [],
        "artifacts": [],
    }
    return orchestrator._compose(task, "")


def test_autonomous_wrapper_exposes_only_structured_step_semantics():
    wrapper = _physical_wrapper()
    strategy = build_execution_strategy(
        wrapper,
        {"open_application", "inspect_desktop_surfaces", "desktop_click"},
    )
    actions = [node.requested_action for node in strategy.mission.action_nodes]
    assert actions == ["open", "calculate"]
    assert sum(node.required_application == "calculator" for node in strategy.mission.action_nodes) == 2
    assert all("autonomous" not in node.observable_success_postcondition.lower()
               for node in strategy.mission.action_nodes)
    first = tool_preflight(
        "open_application", {"name": "Calculator"}, strategy=strategy,
    )
    assert first is not None and first.reason_code == "strategy_selected"
    assert len(strategy.mission.action_nodes) == 2


def test_calculator_structured_plan_has_only_relevant_nonduplicated_nodes():
    phases = _plan(_StructuredAgent([PHYSICAL_INITIAL_PROVIDER_FINAL]))
    tasks = [task for phase in phases for task in phase["tasks"]]
    objectives = [task["objective"] for task in tasks]
    rendered = " ".join(
        task["objective"] + " " + task["success_criterion"] for task in tasks
    ).lower()
    assert "calculator" in rendered
    assert "347" in rendered and "29" in rendered and "10063" in rendered
    assert "step objective" not in rendered
    assert "success criterion" not in rendered
    assert "query_document" not in rendered
    assert sum(text.lower().startswith("open calculator") for text in objectives) == 1
