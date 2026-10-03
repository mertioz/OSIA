"""Focused Unified Chat integration tests for Computer Use V4."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import time

from PIL import Image, ImageDraw
import pytest

from osia import image_inputs, tool_attempts
from osia.agent import OSIAAgent, _execute_tool_with_policy
from osia.chatgpt_account.app_server_client import _canonical_native_tools
from osia.chatgpt_account.provider import ChatGPTAccountProvider
from osia.computer_use_v4_integration import (
    computer_use_v4_test_scope,
    computer_use_v4_turn_scope,
    tool_computer_click,
    tool_computer_observation,
    tool_computer_screenshot,
)
from osia.execution_strategy import (
    StrategyLayer,
    build_execution_strategy,
    execution_strategy_scope,
    tool_preflight,
)
from osia.goal_contract import goal_contract_from_semantic_mission
from osia.tool_attempts import ToolAttemptStore
from osia.tool_attempts import ToolAttemptStatus


class _UnifiedHarness:
    def __init__(self, root: Path):
        self.image = Image.new("RGB", (300, 180), "#111827")
        draw = ImageDraw.Draw(self.image)
        draw.rectangle(
            (170, 95, 270, 145), fill="#D97706", outline="white", width=2,
        )
        draw.line((200, 120, 240, 120), fill="white", width=2)
        self.window = {
            "hwnd": 101,
            "pid": 42,
            "process_started_at": 10.0,
            "window_class": "FixtureClass",
            "window_title": "OSIA Controlled Vision Fixture",
            "window_bounds": (100, 200, 700, 560),
            "client_bounds": (0, 0, 580, 320),
            "client_origin_screen": (110, 230),
            "dpi_scale": 1.0,
            "capture_protection": "",
            "foreground_hwnd": 101,
            "bounds_object": object(),
        }
        self.store = ToolAttemptStore(root)
        self.callbacks = []
        self.captures = 0
        self.activation_calls = []
        self.activation_dispatch_states = []
        self.click_mutation = None

    def window_state(self, hwnd):
        return dict(self.window) if int(hwnd) == 101 else None

    def capture(self, _hwnd, _state):
        self.captures += 1
        return self.image.copy(), "mock_exact_window", {
            "captured_at": time.time(),
            "capture_screen_bounds": [100, 200, 700, 560],
            "capture_attempts": [{
                "capture_driver": "mock_exact_window",
                "outcome": "selected",
                "reason_code": "capture_frame_stable",
            }],
        }

    def click(self, action, point):
        self.callbacks.append((action.kind, point))
        if callable(self.click_mutation):
            self.click_mutation(self.image)
        return {"fixture_state": "CLICKED_ONCE", "callback_count": 1}

    def activate_foreground(self, frame, state):
        self.activation_calls.append((
            frame.hwnd, int(state.get("foreground_hwnd") or 0),
        ))
        dispatch_started = []
        for path in self.store.attempts_dir.glob("attempt_*.json"):
            durable = self.store.get_attempt(path.stem) or {}
            dispatch_started.append(bool(
                (durable.get("action_truth") or {}).get(
                    "dispatch_started_at"
                )
            ))
        self.activation_dispatch_states.append(dispatch_started)
        self.window["foreground_hwnd"] = frame.hwnd
        return {
            "attempted": True,
            "status": "verified",
            "target_hwnd": str(frame.hwnd),
            "target_pid": frame.pid,
            "before_foreground_hwnd": str(
                int(state.get("foreground_hwnd") or 0)
            ),
            "verified_foreground_hwnd": str(frame.hwnd),
            "reason_code": "target_foreground_verified",
            "completed_at": time.time(),
        }

    @staticmethod
    def verifier(result, _action, _frame):
        return bool(
            isinstance(result, dict)
            and result.get("fixture_state") in {
                "CLICKED_ONCE", "SEARCH_SUBMITTED",
            }
        )

    def scope(
        self, *, vision_capable=True, before_action=None,
        verify_effect=True,
    ):
        return computer_use_v4_test_scope(
            vision_capable=vision_capable,
            window_state=self.window_state,
            capture_window=self.capture,
            foreground_activation=self.activate_foreground,
            window_resolver=lambda _window: 101,
            action_store=self.store,
            dispatchers={
                "click": self.click,
                "double_click": self.click,
                "type": lambda action, point: self.callbacks.append(
                    (action.kind, point)
                ) or {"fixture_state": "TYPED"},
                "keypress": lambda action, point: self.callbacks.append(
                    (action.kind, point)
                ) or {"fixture_state": "SEARCH_SUBMITTED"},
            },
            dispatch_result_verifier=(
                self.verifier if verify_effect else None
            ),
            before_action=before_action,
        )


def _attempt_context():
    return {
        "enabled": True,
        "conversation_id": "conversation_wor289_v4",
        "turn_id": "turn_wor289_v4",
        "request_id": "request_wor289_v4",
        "tool_call_id": "call_wor289_v4",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-terra",
        "turn_context": None,
        "owner_scope": "chat",
    }


def _bind_store(monkeypatch, store):
    monkeypatch.setattr(tool_attempts, "_DEFAULT_STORE", store)
    monkeypatch.setattr(tool_attempts, "_DEFAULT_COORDINATOR", None)


def _bind_strategy_contract(
    strategy,
    *,
    conversation_id="conversation_wor289_v4",
    request_id="request_wor289_v4",
    turn_id="turn_wor289_v4",
):
    strategy.bind_goal_contract(goal_contract_from_semantic_mission(
        strategy.mission,
        execution_binding={
            "conversation_id": conversation_id,
            "request_id": request_id,
            "turn_id": turn_id,
            "run_id": turn_id,
            "provider_id": "chatgpt_account",
            "model_id": "gpt-5.6-terra",
            "effective_reasoning": "auto",
        },
    ))
    return strategy


class _ProjectionController:
    def __init__(self, strategy, *, attempt_id=""):
        contract = strategy.goal_contract_dict()
        self._snapshot = {
            "execution_binding": contract["execution_binding"],
            "required_postconditions": list(strategy.required_postconditions),
            "unknown_effects": (
                {attempt_id: {"outcome": "outcome_unknown"}}
                if attempt_id else {}
            ),
        }
        self.proven = []
        self.reconciled = []
        self.typed = []

    def snapshot(self):
        return dict(self._snapshot)

    def prove_postcondition(self, postcondition, evidence):
        self.proven.append((postcondition, evidence))
        return True

    def reconcile_unknown(self, attempt_id, *, outcome, evidence):
        self.reconciled.append((attempt_id, outcome, evidence))

    def record_typed_evidence(self, **values):
        self.typed.append(dict(values))

    def set_strategy_snapshot(self, _snapshot):
        return None


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only V4 native frontier",
)
def test_v4_native_tools_are_exposed_only_for_the_bound_vision_route(tmp_path):
    harness = _UnifiedHarness(tmp_path / "attempts")
    strategy = build_execution_strategy(
        "Clique visuellement le contrôle dans la fenêtre.",
        (
            "computer_screenshot", "computer_click",
            "inspect_desktop_surfaces", "activate_desktop_surface",
        ),
    )
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope(vision_capable=True):
        names = [item["name"] for item in _canonical_native_tools(strategy)]
    assert "computer_screenshot" in names
    assert "computer_observation" in names
    assert "computer_click" in names
    assert "inspect_desktop_surfaces" in names

    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.3-codex-spark",
        conversation_id="conversation_wor289_text",
        turn_id="turn_wor289_text",
    ), harness.scope(vision_capable=False):
        names = [item["name"] for item in _canonical_native_tools(strategy)]
    assert not any(name.startswith("computer_") for name in names)
    assert "inspect_desktop_surfaces" in names
    assert harness.captures == 0


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only V4 native frontier",
)
def test_frozen_v4_reconciliation_keeps_native_visibility_without_legacy_gate(
    tmp_path,
):
    harness = _UnifiedHarness(tmp_path / "attempts-frozen-tools")
    strategy = build_execution_strategy(
        "Dans AppX, clique sur LIBRARY puis tape Subnautica.",
        (
            "computer_screenshot", "computer_observation", "computer_click",
            "computer_type", "computer_keypress", "computer_actions",
        ),
    )
    mechanism = strategy.mechanism_for_layer(
        StrategyLayer.DIRECT_CAPABILITY,
        node_id=strategy.goal.node_id,
    )
    assert mechanism is not None
    decision = strategy.select(mechanism, {"target": "LIBRARY"})
    assert decision.allowed is True
    strategy.record(
        decision,
        "ambiguous",
        failure_reason="desktop_primitive_requires_exact_reconciliation",
    )
    assert strategy.frozen_reason

    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_frozen",
        turn_id="turn_wor289_frozen",
    ), harness.scope(vision_capable=True):
        tools = _canonical_native_tools(strategy)
        names = {item["name"] for item in tools}
        # WOR-300/WOR-305: a frozen legacy strategy does not remove or gate
        # technically available native capabilities. Canonical argument,
        # freshness, permission, and ActionTruth checks still apply at dispatch.
        assert {
            "computer_screenshot", "computer_zoom", "computer_observation",
            "computer_click", "computer_double_click", "computer_type",
            "computer_keypress", "computer_scroll", "computer_actions",
        } <= names
        for tool_name in (
            "computer_click", "computer_type", "computer_keypress",
            "computer_actions",
        ):
            preflight = tool_preflight(tool_name, {}, strategy=strategy)
            assert preflight is None or preflight.allowed is True, tool_name
        for tool_name in (
            "computer_screenshot", "computer_zoom", "computer_observation",
        ):
            assert tool_name in names
            assert tool_preflight(tool_name, {}, strategy=strategy) is None


def test_screenshot_returns_one_minimal_same_turn_image_reference(tmp_path, monkeypatch):
    harness = _UnifiedHarness(tmp_path / "attempts")
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path / "media"))
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope():
        result = tool_computer_screenshot("OSIA Controlled Vision Fixture")
    assert result["visual_frame_id"] == result["receipt"]["frame_id"]
    assert result["receipt"]["provider_id"] == "chatgpt_account"
    assert result["receipt"]["model_id"] == "gpt-5.6-terra"
    assert result["receipt"]["image_width"] == 300
    assert result["receipt"]["image_height"] == 180
    assert result["image_ref"]["owner_scope"] == "turn"
    assert "screenshot" not in result
    assert harness.captures == 1


def test_canonical_visual_report_is_frame_bound_without_strategy(
    tmp_path, monkeypatch,
):
    from osia.execution_strategy import current_execution_strategy

    harness = _UnifiedHarness(tmp_path / "attempts")
    _bind_store(monkeypatch, harness.store)
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path / "media"))
    with (
        computer_use_v4_turn_scope(
            "chatgpt_account", "gpt-5.6-terra",
            conversation_id="conversation_wor289_v4",
            turn_id="turn_wor289_v4",
        ),
        harness.scope(),
        execution_strategy_scope("", (), enabled=False),
    ):
        assert current_execution_strategy() is None
        screenshot = tool_computer_screenshot("OSIA Controlled Vision Fixture")
        record = _execute_tool_with_policy(
            "computer_observation",
            {
                "frame_id": screenshot["receipt"]["frame_id"],
                "status": "satisfied",
                "observed_state": "The amber control is visible.",
            },
            user_request="Inspect the current fixture image.",
            attempt_context=_attempt_context(),
        )

    assert record.dispatched is True
    assert record.contract.spec.name == "computer_observation"
    receipt = record.result["receipt"]
    assert receipt["reason_code"] == "v4_frame_bound_model_report"
    assert receipt["frame_id"] == screenshot["receipt"]["frame_id"]
    assert receipt["image_sha256"] == screenshot["receipt"]["image_sha256"]
    assert receipt["reported_status"] == "satisfied"
    assert receipt["authority"] == "model_report_only"
    assert receipt["authoritative"] is False
    assert "not independent verification" in record.result["text"]
    assert harness.callbacks == []


def test_app_server_delivers_v4_pixels_and_continues_on_same_native_model(
    tmp_path, monkeypatch,
):
    from tests.test_wor266_unified_chat_vision_seam import (
        _UnifiedChatAppServer,
        _response_payload,
    )
    from unittest.mock import patch
    from osia.execution_strategy import (
        current_execution_strategy,
        execution_strategy_scope,
    )

    class _V4ImageServer(_UnifiedChatAppServer):
        def _simulate(self, thread_id):
            self._dispatch_notification(
                "turn/started",
                {"threadId": thread_id, "turn": {"id": "turn-wor266-chat"}},
            )
            self._dynamic_call(
                thread_id, 701, "call-v4-screen",
                "computer_screenshot", {"window": "Fixture"},
            )
            self._finish(thread_id, "The same model continued after the V4 image.")

    harness = _UnifiedHarness(tmp_path / "attempts")
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path / "media"))
    server = _V4ImageServer("assistant_item")
    provider = ChatGPTAccountProvider(client=server)
    chat = provider.chat_client("conv-wor266-chat")
    chat.set_tool_attempt_request_id("request-wor266-chat")
    chat.set_tool_attempt_turn_id("turn-wor266-chat")
    with (
        computer_use_v4_turn_scope(
            "chatgpt_account", "gpt-5.6-terra",
            conversation_id="conv-wor266-chat",
            turn_id="turn-wor266-chat",
        ),
        harness.scope(),
        patch(
            "osia.model_capabilities._exact_account_metadata",
            return_value={
                "id": "gpt-5.6-terra",
                "provider": "chatgpt_account",
                "inputModalities": ["text", "image"],
            },
        ),
        execution_strategy_scope(
            "Inspect the controlled fixture visually.", (), enabled=False,
        ),
    ):
        assert current_execution_strategy() is None
        response = chat.chat.completions.create(
            model="gpt-5.6-terra",
            messages=[{
                "role": "user",
                "content": "Inspect the controlled fixture visually.",
            }],
        )
    assert response.choices[0].message.content.startswith("The same model")
    assert server.turn_models == ["gpt-5.6-terra"]
    assert [item["name"] for item in server.advertised_tools].count(
        "computer_screenshot"
    ) == 1
    sent = next(item for item in server.sent if item.get("id") == 701)
    assert sum(
        item.get("type") == "inputImage"
        for item in sent["result"]["contentItems"]
    ) == 1
    payload = _response_payload(sent)
    assert payload["tool"] == "computer_screenshot"
    assert payload["reason_code"] == "IMAGE_INPUT_SENT"
    assert len(server.steers) == 1
    events = chat.consume_capability_events()
    consumed = next(
        event
        for event in events
        if event.tool_name == "computer_screenshot"
        and event.reason_code == "IMAGE_INPUT_CONSUMED"
    )
    assert consumed is not None
    assert consumed.outcome == "success_verified"
    assert consumed.reason_code == "IMAGE_INPUT_CONSUMED"
    assert any(
        event.tool_name == "computer_screenshot"
        and event.reason_code == "IMAGE_INPUT_CONSUMED"
        for event in events
    )


def test_successful_image_input_marker_is_not_projected_as_typed_failure():
    from osia.agent import OSIAAgent

    agent = object.__new__(OSIAAgent)
    agent.lang = "fr"
    final = agent._terminal_final_text(
        "blocked_verified",
        "",
        snapshot={
            "strategy_snapshot": {
                "mission": {
                    "action_nodes": [{
                        "source_clause": "clique sur library",
                        "requested_action": "activate",
                        "target_object": "library",
                        "required_postconditions": [
                            "library is visibly selected",
                        ],
                    }],
                },
            },
            "typed_evidence": [
                {
                    "outcome": "success_verified",
                    "reason_code": "IMAGE_INPUT_SENT",
                },
                {
                    "outcome": "confirmed_failure_before_effect",
                    "reason_code": "target_window_not_foreground",
                },
            ],
        },
    )

    assert "IMAGE_INPUT_SENT" not in final
    assert "défaillance typée" not in final
    assert "Aucun moyen valide" in final


def test_real_policy_wrapper_reuses_one_actiontruth_attempt_at_v4_boundary(
    tmp_path, monkeypatch,
):
    harness = _UnifiedHarness(tmp_path / "attempts")
    _bind_store(monkeypatch, harness.store)
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope():
        screenshot = tool_computer_screenshot("Fixture")
        frame_id = screenshot["receipt"]["frame_id"]
        record = _execute_tool_with_policy(
            "computer_click",
            {"frame_id": frame_id, "x": 220, "y": 120, "intent": "click test"},
            user_request="Clique le contrôle exactement une fois.",
            attempt_context=_attempt_context(),
        )
    assert record.outcome.value == "success_verified"
    assert record.dispatched is True
    assert harness.callbacks == [("click", (540, 440))]
    attempts = harness.store.list_action_attempts(
        (harness.store.get_attempt(record.tool_attempt_id)["action_truth"])[
            "action_id"
        ]
    )
    assert len(attempts) == 1
    assert attempts[0]["action_truth"]["dispatch_started_at"] > 0
    assert attempts[0]["action_truth"]["effect_truth"] == "verified"


def test_foreground_preparation_precedes_policy_actiontruth_dispatch(
    tmp_path, monkeypatch,
):
    harness = _UnifiedHarness(tmp_path / "attempts")
    _bind_store(monkeypatch, harness.store)
    harness.window["foreground_hwnd"] = 202
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope():
        screenshot = tool_computer_screenshot("Fixture")
        record = _execute_tool_with_policy(
            "computer_click",
            {
                "frame_id": screenshot["receipt"]["frame_id"],
                "x": 220,
                "y": 120,
                "intent": "click test",
            },
            user_request="Clique le contrôle exactement une fois.",
            attempt_context=_attempt_context(),
        )

    assert record.outcome.value == "success_verified"
    assert record.dispatched is True
    assert harness.activation_calls == [(101, 202)]
    assert harness.activation_dispatch_states == [[False]]
    receipt = record.result["receipt"]
    preparation = receipt["foreground_preparation"]
    assert preparation["status"] == "verified"
    assert preparation["verified_foreground_hwnd"] == "101"
    durable = harness.store.get_attempt(record.tool_attempt_id)
    assert durable["action_truth"]["dispatch_started_at"] >= preparation[
        "completed_at"
    ]
    assert harness.callbacks == [("click", (540, 440))]


def test_failed_foreground_preparation_is_typed_for_same_model_continuation(
    tmp_path,
):
    harness = _UnifiedHarness(tmp_path / "attempts")
    harness.window["foreground_hwnd"] = 202
    harness.activate_foreground = lambda _frame, state: {
        "attempted": True,
        "status": "failed",
        "before_foreground_hwnd": str(state["foreground_hwnd"]),
        "verified_foreground_hwnd": str(state["foreground_hwnd"]),
        "reason_code": "target_foreground_activation_failed",
    }
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope():
        screenshot = tool_computer_screenshot("Fixture")
        result = tool_computer_click(
            screenshot["receipt"]["frame_id"],
            220,
            120,
            "click test",
        )

    assert result["receipt"]["dispatch_state"] == "not_dispatched"
    assert result["receipt"]["outcome"] == "confirmed_failure_before_effect"
    assert result["receipt"]["v4_status"] == (
        "FOREGROUND_PREPARATION_FAILED"
    )
    assert result["receipt"]["reason_code"] == (
        "target_foreground_activation_failed"
    )
    assert result["receipt"]["foreground_preparation"]["status"] == "failed"
    assert harness.callbacks == []


def test_stale_result_returns_before_actiontruth_claim_and_reobserves(
    tmp_path, monkeypatch,
):
    harness = _UnifiedHarness(tmp_path / "attempts")
    _bind_store(monkeypatch, harness.store)

    def stale_once(_tool, _arguments):
        draw = ImageDraw.Draw(harness.image)
        draw.rectangle((168, 93, 272, 147), fill="#111827")
        draw.rectangle(
            (30, 40, 130, 90), fill="#D97706", outline="white", width=2,
        )
        draw.line((60, 65, 100, 65), fill="white", width=2)

    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope(before_action=stale_once):
        first = tool_computer_screenshot("Fixture")
        frame_id = first["receipt"]["frame_id"]
        record = _execute_tool_with_policy(
            "computer_click",
            {"frame_id": frame_id, "x": 220, "y": 120, "intent": "click test"},
            user_request="Clique le contrôle exactement une fois.",
            attempt_context=_attempt_context(),
        )
        second = tool_computer_screenshot("Fixture")
    assert record.outcome.value == "confirmed_failure_before_effect"
    assert record.reason_code in {
        "target_structure_moved", "target_structure_changed",
    }
    assert harness.callbacks == []
    durable = harness.store.get_attempt(record.tool_attempt_id)
    assert durable["action_truth"]["dispatch_started_at"] == 0.0
    assert second["receipt"]["frame_id"] != frame_id


def test_bounded_multi_action_keeps_outer_claim_and_primitive_identities(
    tmp_path, monkeypatch,
):
    harness = _UnifiedHarness(tmp_path / "attempts")
    _bind_store(monkeypatch, harness.store)
    request = (
        "Dans AppX, clique dans la barre de recherche et tape Subnautica. "
        "Vérifie visuellement que Subnautica est présent dans le champ."
    )
    strategy = _bind_strategy_contract(build_execution_strategy(
        request,
        {"computer_screenshot", "computer_observation", "computer_actions"},
    ))
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope(), execution_strategy_scope(
        request, strategy=strategy,
    ):
        screenshot = tool_computer_screenshot("Fixture")
        frame_id = screenshot["receipt"]["frame_id"]
        actions = [
            {
                "kind": "click", "frame_id": frame_id,
                "x": 220, "y": 120, "effect_id": "focus field",
            },
            {
                "kind": "type", "frame_id": frame_id,
                "text": "Subnautica", "effect_id": "type query",
            },
            {
                "kind": "keypress", "frame_id": frame_id,
                "key": "enter", "effect_id": "submit query",
                "observe_after": True,
            },
        ]
        decision = tool_preflight(
            "computer_actions", {"actions": actions}, strategy=strategy,
        )
        assert decision is not None and decision.allowed is True
        agent = OSIAAgent.__new__(OSIAAgent)
        agent._tool_attempt_metadata = {
            "conversation_id": "conversation_wor289_v4",
            "turn_id": "turn_wor289_v4",
            "request_id": "request_wor289_v4",
            "owner_scope": "chat",
        }
        agent.provider = "chatgpt_account"
        agent.model = "gpt-5.6-terra"
        agent.turn_context = None
        agent.client = SimpleNamespace(conversation_id="conversation_wor289_v4")
        attempt_context = agent._new_tool_attempt_context(
            tool_name="computer_actions",
            arguments={"actions": actions},
            strategy_decision=decision,
        )
        record = _execute_tool_with_policy(
            "computer_actions",
            {"actions": actions},
            user_request=request,
            attempt_context=attempt_context,
        )
    assert record.outcome.value == "success_verified"
    assert [item[0] for item in harness.callbacks] == [
        "click", "type", "keypress",
    ]
    durable = harness.store.get_attempt(record.tool_attempt_id)
    assert durable["action_truth"]["dispatch_started_at"] > 0
    assert len(harness.store.list_action_attempts(
        durable["action_truth"]["action_id"]
    )) == 1
    journal = durable["primitive_journal"]
    assert len(journal) == 3
    assert len({item["action_id"] for item in journal}) == 3
    assert len({item["semantic_effect_id"] for item in journal}) == 3
    assert [item["uia_pattern"] for item in journal] == [
        "computer_click", "computer_type", "computer_keypress",
    ]
    assert all(item["lifecycle"] == "verified" for item in journal)


def test_exact_final_batch_proof_verifies_unknown_dispatched_primitives(
    tmp_path,
):
    store = ToolAttemptStore(tmp_path / "attempts")
    attempt = store.create_attempt(
        conversation_id="conversation_wor289_batch",
        turn_id="turn_wor289_batch",
        request_id="request_wor289_batch",
        tool_call_id="call_wor289_batch",
        tool_name="computer_actions",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context=None,
        argument_fingerprint=store.fingerprint_arguments({"batch": "safe"}),
        side_effect="reversible_local",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        primitive_binding={
            "semantic_batch_id": "v4batch_fixture",
            "batch_primitives": [
                {
                    "semantic_node_id": "node_input",
                    "semantic_substep_id": "node_input.batch.1",
                    "strategy_attempt_id": "strategy_input",
                    "application_identity": "v4_fixture",
                    "surface_ref": "v4_fixture",
                    "native_window_id": "101",
                    "window_generation": "generation_fixture",
                    "uia_pattern": "computer_click",
                    "attempted_effect": "focus_field",
                    "semantic_postcondition": "sha256:" + "1" * 64,
                },
                {
                    "semantic_node_id": "node_input",
                    "semantic_substep_id": "node_input.batch.2",
                    "strategy_attempt_id": "strategy_input",
                    "application_identity": "v4_fixture",
                    "surface_ref": "v4_fixture",
                    "native_window_id": "101",
                    "window_generation": "generation_fixture",
                    "uia_pattern": "computer_type",
                    "attempted_effect": "type_query",
                    "semantic_postcondition": "sha256:" + "2" * 64,
                },
            ],
        },
        action_binding={
            "semantic_node_id": "node_input",
            "attempted_effect": "computer_use_v4_semantic",
            "operation": "computer_actions",
            "target": {
                "kind": "desktop",
                "surface_ref": "v4_fixture",
                "window_generation": "generation_fixture",
            },
        },
    )
    attempt_id = attempt["tool_attempt_id"]
    store.mark_accepted(attempt_id)
    store.arm_owner_dispatch_boundary(attempt_id)
    store.mark_running(attempt_id)
    _claimed, claimed = store.claim_owner_dispatch(attempt_id)
    assert claimed is True
    for primitive in store.get_attempt(attempt_id)["primitive_journal"]:
        store.record_primitive_dispatch_started(
            attempt_id, primitive_id=primitive["primitive_id"],
        )
        store.record_primitive_dispatch_returned(
            attempt_id, primitive_id=primitive["primitive_id"],
        )
    store.record_terminal_result(
        attempt_id,
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
        reason_code="v4_post_action_observation_required",
    )
    unknown = store.get_attempt(attempt_id)
    assert all(
        item["lifecycle"] in {"dispatch_returned", "unknown"}
        for item in unknown["primitive_journal"]
    )

    verified = store.update_action_truth(
        attempt_id,
        state="effect_verified",
        reason_code="v4_visual_postcondition_verified",
        evidence={"evidence_ref": "sha256:" + "3" * 64},
        reconciliation_status="verified",
        exact=True,
    )

    assert verified["action_truth"]["effect_truth"] == "verified"
    assert verified["semantic_batch"]["state"] == "verified"
    assert all(
        item["lifecycle"] == "verified"
        and item["effect_classification"] == "verified"
        for item in verified["primitive_journal"]
    )
    preserved = store.record_primitive_outcome(
        attempt_id,
        primitive_id=verified["primitive_journal"][0]["primitive_id"],
        outcome="unknown",
    )
    assert all(
        item["lifecycle"] == "verified"
        for item in preserved["primitive_journal"]
    )


def test_final_batch_proof_cannot_verify_an_undispatched_suffix(tmp_path):
    store = ToolAttemptStore(tmp_path / "attempts")
    attempt = store.create_attempt(
        conversation_id="conversation_wor289_partial",
        turn_id="turn_wor289_partial",
        request_id="request_wor289_partial",
        tool_call_id="call_wor289_partial",
        tool_name="computer_actions",
        provider_id="chatgpt_account",
        model_id="gpt-5.6-terra",
        turn_context=None,
        argument_fingerprint=store.fingerprint_arguments({"batch": "partial"}),
        side_effect="reversible_local",
        idempotence="non_idempotent",
        retry_policy="never_after_dispatch",
        primitive_binding={
            "semantic_batch_id": "v4batch_partial",
            "batch_primitives": [
                {
                    "semantic_node_id": "node_input",
                    "semantic_substep_id": "node_input.batch.1",
                    "strategy_attempt_id": "strategy_input",
                    "application_identity": "v4_fixture",
                    "surface_ref": "v4_fixture",
                    "native_window_id": "101",
                    "window_generation": "generation_fixture",
                    "uia_pattern": "computer_click",
                    "attempted_effect": "focus_field",
                    "semantic_postcondition": "sha256:" + "1" * 64,
                },
                {
                    "semantic_node_id": "node_input",
                    "semantic_substep_id": "node_input.batch.2",
                    "strategy_attempt_id": "strategy_input",
                    "application_identity": "v4_fixture",
                    "surface_ref": "v4_fixture",
                    "native_window_id": "101",
                    "window_generation": "generation_fixture",
                    "uia_pattern": "computer_type",
                    "attempted_effect": "type_query",
                    "semantic_postcondition": "sha256:" + "2" * 64,
                },
            ],
        },
        action_binding={
            "semantic_node_id": "node_input",
            "attempted_effect": "computer_use_v4_semantic",
            "operation": "computer_actions",
            "target": {
                "kind": "desktop",
                "surface_ref": "v4_fixture",
                "window_generation": "generation_fixture",
            },
        },
    )
    attempt_id = attempt["tool_attempt_id"]
    store.mark_accepted(attempt_id)
    store.arm_owner_dispatch_boundary(attempt_id)
    store.mark_running(attempt_id)
    _claimed, claimed = store.claim_owner_dispatch(attempt_id)
    assert claimed is True
    first = store.get_attempt(attempt_id)["primitive_journal"][0]
    store.record_primitive_dispatch_started(
        attempt_id, primitive_id=first["primitive_id"],
    )
    store.record_primitive_dispatch_returned(
        attempt_id, primitive_id=first["primitive_id"],
    )

    rejected = store.update_action_truth(
        attempt_id,
        state="effect_verified",
        reason_code="v4_visual_postcondition_verified",
        evidence={"evidence_ref": "sha256:" + "3" * 64},
        reconciliation_status="verified",
        exact=True,
    )

    assert rejected["action_truth"]["effect_truth"] != "verified"
    assert rejected["primitive_journal"][1]["lifecycle"] == "prepared"


def test_v4_schemas_keep_structured_uia_available_and_dispatch_ungated():
    from osia.action_policy import guard_tool_call
    from osia.tool_contracts import (
        ConfirmationPolicy,
        SideEffectClass,
        effective_contract,
        get_tool_spec,
    )

    screenshot = get_tool_spec("computer_screenshot")
    click = get_tool_spec("computer_click")
    enter = get_tool_spec("computer_keypress")
    structured = get_tool_spec("activate_desktop_surface")
    assert screenshot.side_effect is SideEffectClass.READ_ONLY
    # WOR-301: a read-only V4 observation dispatches with no permit.
    assert guard_tool_call(
        "Observe the current window", "computer_screenshot", {"window": "Fixture"},
    ) is None
    assert effective_contract(click, {
        "frame_id": "v4frame_fixture", "x": 1, "y": 1, "intent": "focus field",
    }).confirmation is ConfirmationPolicy.NO_CONFIRMATION
    assert effective_contract(enter, {
        "frame_id": "v4frame_fixture", "key": "enter",
    }).confirmation is ConfirmationPolicy.FINAL_COMMIT_CONFIRMATION
    assert structured is not None
    assert not hasattr(structured, "chat_eligible")


def test_strategy_stale_reopens_only_after_fresh_v4_observation(tmp_path):
    from osia.agent import TOOL_MAP
    from osia.execution_strategy import (
        build_execution_strategy,
        execution_strategy_scope,
        record_tool_outcome,
        tool_preflight,
    )

    request = "Clique visuellement le bouton de la fenêtre contrôlée."
    strategy = build_execution_strategy(request, tuple(TOOL_MAP))
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_stale_strategy",
        turn_id="turn_stale_strategy",
    ), computer_use_v4_test_scope(vision_capable=True), execution_strategy_scope(
        request, tuple(TOOL_MAP), strategy=strategy,
    ):
        first = tool_preflight(
            "computer_click", {"frame_id": "v4frame_old", "x": 1, "y": 1},
        )
        assert first is not None and first.allowed
        record_tool_outcome(
            "computer_click",
            first,
            outcome="confirmed_failure_before_effect",
            is_error=True,
            reason_code="target_structure_changed",
            result={},
        )
        blocked = tool_preflight(
            "computer_click", {"frame_id": "v4frame_new", "x": 2, "y": 2},
        )
        assert blocked is not None and not blocked.allowed
        assert blocked.reason_code == "stale_target_requires_reresolution"
        strategy.observe(
            "fresh Computer Use V4 frame available",
            evidence_token="computer_use_v4_frame:fresh",
        )
        fresh = tool_preflight(
            "computer_click", {"frame_id": "v4frame_new", "x": 2, "y": 2},
        )
        assert fresh is not None and fresh.allowed


def test_v4_frame_identity_does_not_replace_required_semantic_surface():
    from osia.agent import _active_chat_tool_names
    from osia.execution_strategy import (
        build_execution_strategy,
        execution_strategy_scope,
        tool_preflight,
    )

    request = "sur steam clique dans library"
    tools = _active_chat_tool_names()
    strategy = build_execution_strategy(request, tools)
    assert strategy.goal.required_application == "steam"
    assert strategy.goal.required_surface == "library"
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="steam_surface_binding",
        turn_id="steam_surface_binding_turn",
    ), computer_use_v4_test_scope(vision_capable=True), execution_strategy_scope(
        request, tools, strategy=strategy,
    ):
        decision = tool_preflight(
            "computer_click",
            {"frame_id": "v4frame_steam", "x": 238, "y": 59},
        )

    assert decision is not None and decision.allowed
    attempt = strategy.attempt_by_id(decision.attempt_id)
    assert attempt is not None
    assert attempt.target_surface == "library"


def _record_consumed_v4_frame(strategy, screenshot, *, call_id="call-screen"):
    from osia.execution_strategy import record_tool_outcome

    screenshot["receipt"] = dict(screenshot["receipt"])
    screenshot["receipt"].update({
        "reason_code": "IMAGE_INPUT_SENT",
        "image_input_sent": True,
        "tool_call_id": call_id,
    })
    record_tool_outcome(
        "computer_screenshot",
        None,
        outcome="success_verified",
        is_error=False,
        reason_code="IMAGE_INPUT_SENT",
        result=screenshot,
        strategy=strategy,
    )
    assert strategy.mark_visual_input_consumed(
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        tool_call_id=call_id,
    )


def test_fresh_current_v4_evidence_can_prove_already_satisfied(
    tmp_path, monkeypatch,
):
    from osia.agent import OSIAAgent, _active_chat_tool_names
    from osia.execution_strategy import execution_strategy_scope

    harness = _UnifiedHarness(tmp_path / "attempts")
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path / "media"))
    request = "Clique sur Library dans Steam."
    strategy = _bind_strategy_contract(build_execution_strategy(
        request, _active_chat_tool_names(),
    ))
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope(), execution_strategy_scope(
        request, _active_chat_tool_names(), strategy=strategy,
    ):
        screenshot = tool_computer_screenshot("Fixture")
        _record_consumed_v4_frame(strategy, screenshot)
        observation = tool_computer_observation(
            screenshot["receipt"]["frame_id"],
            "satisfied",
            "LIBRARY is visibly selected in the current frame.",
        )
        controller = _ProjectionController(strategy)
        agent = object.__new__(OSIAAgent)
        projected = agent._project_v4_grounded_observation(
            controller,
            strategy,
            segment_strategy="provider_initial",
            segment_postcondition="",
        )

    postcondition = strategy.goal.required_postconditions[0]
    assert observation["receipt"]["evidence_status"] == "satisfied"
    assert "observed_state" not in observation["receipt"]
    assert observation["receipt"]["observed_state_ref"].startswith("sha256:")
    staged = strategy.v4_grounded_observation()
    assert "observed_state" not in staged
    assert staged["observed_state_ref"].startswith("sha256:")
    assert projected is True
    assert postcondition in strategy.proven_postconditions
    assert controller.proven[0][0] == postcondition
    assert harness.callbacks == []


def test_wrong_or_inconclusive_v4_evidence_never_proves_success(
    tmp_path, monkeypatch,
):
    from osia.agent import OSIAAgent, _active_chat_tool_names
    from osia.execution_strategy import execution_strategy_scope

    for status in ("not_satisfied", "inconclusive"):
        harness = _UnifiedHarness(tmp_path / status)
        monkeypatch.setattr(
            image_inputs, "CONFIG_DIR", str(tmp_path / status / "media"),
        )
        request = "Clique sur Library dans Steam."
        strategy = _bind_strategy_contract(build_execution_strategy(
            request, _active_chat_tool_names(),
        ))
        with computer_use_v4_turn_scope(
            "chatgpt_account", "gpt-5.6-terra",
            conversation_id="conversation_wor289_v4",
            turn_id="turn_wor289_v4",
        ), harness.scope(), execution_strategy_scope(
            request, _active_chat_tool_names(), strategy=strategy,
        ):
            screenshot = tool_computer_screenshot("Fixture")
            _record_consumed_v4_frame(strategy, screenshot)
            tool_computer_observation(
                screenshot["receipt"]["frame_id"],
                status,
                "The requested selected state is not established.",
            )
            controller = _ProjectionController(strategy)
            agent = object.__new__(OSIAAgent)
            projected = agent._project_v4_grounded_observation(
                controller,
                strategy,
                segment_strategy="provider_initial",
                segment_postcondition="",
            )
        assert projected is False
        assert strategy.proven_postconditions == {}
        assert controller.proven == []
        assert controller.typed[-1]["reason_code"].startswith(
            "v4_visual_postcondition_"
        )


def test_post_action_consumed_v4_evidence_reconciles_actiontruth_once(
    tmp_path, monkeypatch,
):
    from osia.agent import OSIAAgent, _active_chat_tool_names
    from osia.execution_strategy import execution_strategy_scope

    harness = _UnifiedHarness(tmp_path / "attempts")
    _bind_store(monkeypatch, harness.store)
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path / "media"))
    harness.click_mutation = lambda image: ImageDraw.Draw(image).text(
        (15, 15), "LIBRARY_SELECTED", fill="white",
    )
    request = "Clique sur Library dans Steam."
    tools = _active_chat_tool_names()
    strategy = _bind_strategy_contract(build_execution_strategy(
        request, tools,
    ))
    with computer_use_v4_turn_scope(
        "chatgpt_account", "gpt-5.6-terra",
        conversation_id="conversation_wor289_v4",
        turn_id="turn_wor289_v4",
    ), harness.scope(verify_effect=False), execution_strategy_scope(
        request, tools, strategy=strategy,
    ):
        before = tool_computer_screenshot("Fixture")
        action_arguments = {
            "frame_id": before["receipt"]["frame_id"],
            "x": 220,
            "y": 120,
            "intent": "select Library",
        }
        from osia.execution_strategy import tool_preflight

        decision = tool_preflight(
            "computer_click", action_arguments, strategy=strategy,
        )
        assert decision is not None and decision.allowed
        from osia.computer_use_v4_integration import (
            computer_use_v4_action_target,
        )

        context = _attempt_context()
        context["action_binding"] = {
            "operation": "computer_click",
            "attempted_effect": decision.attempted_effect,
            "semantic_node_id": decision.node_id,
            "target": computer_use_v4_action_target(
                "computer_click", action_arguments,
            ),
        }
        action = _execute_tool_with_policy(
            "computer_click",
            action_arguments,
            user_request=request,
            attempt_context=context,
        )
        assert action.outcome.value == "outcome_unknown"
        from osia.execution_strategy import record_tool_outcome
        from osia.tool_attempts import ToolAttemptStatus

        durable_action, _created = harness.store.record_terminal_result(
            action.tool_attempt_id,
            status=ToolAttemptStatus.UNKNOWN_AFTER_START,
            terminal_outcome="outcome_unknown",
            dispatch_state="effect_unknown",
            reason_code="postcondition_not_observed",
        )
        assert durable_action.get("status") == "unknown_after_start"
        if not strategy.frozen_reason:
            record_tool_outcome(
                "computer_click",
                decision,
                outcome="outcome_unknown",
                is_error=False,
                reason_code="postcondition_not_observed",
                result={},
                strategy=strategy,
                durable_attempt_id=action.tool_attempt_id,
            )
        assert strategy.frozen_reason
        after = tool_computer_screenshot("Fixture")
        _record_consumed_v4_frame(
            strategy, after, call_id="call-post-action",
        )
        grounded = tool_computer_observation(
            after["receipt"]["frame_id"],
            "satisfied",
            "LIBRARY is visibly selected after the click.",
        )
        assert grounded["receipt"]["reason_code"] == (
            "v4_grounded_visual_evidence_staged"
        )
        controller = _ProjectionController(
            strategy, attempt_id=action.tool_attempt_id,
        )
        agent = object.__new__(OSIAAgent)
        projected = agent._project_v4_grounded_observation(
            controller,
            strategy,
            segment_strategy="reconcile_unknown_read_only",
            segment_postcondition="reconcile:" + action.tool_attempt_id,
        )

    durable = harness.store.get_attempt(action.tool_attempt_id)
    assert projected is True
    assert durable["action_truth"]["effect_truth"] == "verified"
    assert durable["action_truth"]["reconciliation_status"] == "verified"
    assert len(harness.callbacks) == 1
    assert controller.reconciled[0][0] == action.tool_attempt_id


def test_previous_chat_v4_frame_cannot_prove_current_chat_state():
    from osia.agent import _active_chat_tool_names

    strategy = _bind_strategy_contract(
        build_execution_strategy(
            "Clique sur Library dans Steam.", _active_chat_tool_names(),
        ),
        conversation_id="conversation-current",
        request_id="request-current",
        turn_id="turn-current",
    )
    postcondition = strategy.goal.required_postconditions[0]
    strategy._latest_desktop_observation["visual_evidence_history"] = [{
        "frame_id": "v4frame_previous",
        "conversation_id": "conversation-previous",
        "turn_id": "turn-previous",
        "image_input_sent": True,
        "image_input_consumed": True,
    }]
    assert not strategy.stage_v4_grounded_observation({
        "conversation_id": "conversation-previous",
        "turn_id": "turn-previous",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-terra",
        "frame_id": "v4frame_previous",
        "postcondition": postcondition,
        "status": "satisfied",
        "observed_state": "Library was selected in the previous chat.",
    })
    assert strategy.proven_postconditions == {}


def test_wrong_window_v4_evidence_is_rejected():
    from osia.agent import _active_chat_tool_names

    strategy = _bind_strategy_contract(build_execution_strategy(
        "Clique sur Library dans Steam.", _active_chat_tool_names(),
    ))
    binding = strategy.goal_contract_dict()["execution_binding"]
    postcondition = strategy.goal.required_postconditions[0]
    strategy._latest_desktop_observation["visual_evidence_history"] = [{
        "frame_id": "v4frame_current",
        "v4_loop_id": "v4loop_current",
        "conversation_id": binding["conversation_id"],
        "turn_id": binding["turn_id"],
        "provider_id": binding["provider_id"],
        "model_id": binding["model_id"],
        "window_generation": "generation_current",
        "native_window_id": "101",
        "image_input_sent": True,
        "image_input_consumed": True,
    }]
    assert not strategy.stage_v4_grounded_observation({
        "conversation_id": binding["conversation_id"],
        "turn_id": binding["turn_id"],
        "provider_id": binding["provider_id"],
        "model_id": binding["model_id"],
        "v4_loop_id": "v4loop_current",
        "frame_id": "v4frame_current",
        "window_generation": "generation_recycled",
        "native_window_id": "202",
        "postcondition": postcondition,
        "status": "satisfied",
        "observed_state": "A different window reports Library.",
    })


def test_structured_application_label_does_not_prove_activation_state(
    monkeypatch,
):
    from osia import desktop_scene
    from osia.agent import _active_chat_tool_names
    from osia.execution_strategy import execution_strategy_scope
    from types import SimpleNamespace

    request = "Clique sur Library dans la fenêtre Steam."
    strategy = build_execution_strategy(request, _active_chat_tool_names())
    scene = SimpleNamespace(surfaces=[SimpleNamespace(
        enabled=True,
        accessible_label="Steam",
        selected=False,
        focused=False,
    )])
    monkeypatch.setattr(
        desktop_scene, "scene_matches_application", lambda *_args: True,
    )
    with execution_strategy_scope(
        request, _active_chat_tool_names(), strategy=strategy,
    ):
        observed = desktop_scene._exact_observed_postcondition(
            scene, "Steam",
        )
    assert observed == ("", "")
    assert strategy.proven_postconditions == {}


def test_exact_selected_structured_target_can_prove_current_state(monkeypatch):
    from osia import desktop_scene
    from osia.agent import _active_chat_tool_names
    from osia.execution_strategy import execution_strategy_scope
    from types import SimpleNamespace

    request = "Clique sur Library dans la fenêtre Steam."
    strategy = build_execution_strategy(request, _active_chat_tool_names())
    scene = SimpleNamespace(surfaces=[SimpleNamespace(
        enabled=True,
        accessible_label="Library",
        selected=True,
        focused=False,
    )])
    monkeypatch.setattr(
        desktop_scene, "scene_matches_application", lambda *_args: True,
    )
    with execution_strategy_scope(
        request, _active_chat_tool_names(), strategy=strategy,
    ):
        postcondition, state = desktop_scene._exact_observed_postcondition(
            scene, "Library",
        )
    assert postcondition == strategy.goal.observable_success_postcondition
    assert state == "exact_postcondition"
