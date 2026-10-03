"""WOR-266 Unified Chat capability/result seam regressions."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from osia import desktop_scene, image_inputs, tool_contracts
from osia.agent import OSIAAgent, TOOL_MAP
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    AppServerError,
    CapabilityEvent,
)
from osia.chatgpt_account.provider import (
    AccountProviderError,
    ChatGPTAccountProvider,
)
from osia.execution_strategy import (
    build_execution_strategy,
    execution_strategy_scope,
)
from osia.goal_contract import goal_contract_from_semantic_mission
from osia.image_inputs import (
    ImageSourceType,
    create_image_artifact,
    decode_image_data_url,
    register_image_artifact,
    turn_scoped_media_owner,
)
from osia.tool_attempts import ToolAttemptStatus, ToolAttemptStore
from osia.turn_terminality import TurnTerminalityController


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
REQUEST = "tu vois quoi sur la fenetre msi center ?"
MODEL = "gpt-5.6-terra"


@pytest.fixture(autouse=True)
def _isolated_media_store(tmp_path, monkeypatch):
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path))
    with image_inputs._IMAGE_STORE_LOCK:
        image_inputs._IMAGE_STORE.clear()
        image_inputs._IMAGE_STORE_BYTES = 0
    yield
    with image_inputs._IMAGE_STORE_LOCK:
        image_inputs._IMAGE_STORE.clear()
        image_inputs._IMAGE_STORE_BYTES = 0


class _UnifiedChatAppServer(AppServerClient):
    """In-process App Server transcript with real client queue/correlation code."""

    def __init__(self, scenario: str):
        self._attempt_tempdir = tempfile.TemporaryDirectory(
            prefix="osia-wor266-app-server-",
        )
        self._test_attempt_store = ToolAttemptStore(
            Path(self._attempt_tempdir.name) / "attempts",
        )
        super().__init__(tool_attempt_store=self._test_attempt_store)
        self._process = SimpleNamespace(poll=lambda: None)
        self.scenario = scenario
        self.advertised_tools = []
        self.turn_models = []
        self.sent = []
        self.requests = []
        self.steers = []
        self._condition = threading.Condition()
        self._responses = {}
        self._simulator = None
        self._early_successor_sent = False

    def start_or_resume_conversation(self, **kwargs):
        self.advertised_tools = list(kwargs.get("dynamic_tools") or ())
        return "thread-wor266-chat"

    def _request(self, method, params=None, **_kwargs):
        params = dict(params or {})
        if method == "turn/steer":
            if self.scenario == "bridge_failed":
                raise AppServerError(
                    "bridge rejected", category="request_rejected",
                )
            assert params.get("threadId") == "thread-wor266-chat"
            assert params.get("expectedTurnId") == "turn-wor266-chat"
            self.steers.append(params)
            return {"turnId": "turn-wor266-chat"}
        assert method == "turn/start"
        self.turn_models.append(str(params.get("model") or ""))
        self._simulator = threading.Thread(
            target=self._simulate,
            args=(str(params.get("threadId") or ""),),
            daemon=True,
        )
        self._simulator.start()
        return {"turn": {"id": "turn-wor266-chat"}}

    def _send(self, message):
        copied = json.loads(json.dumps(message))
        if self.scenario == "response_transport_failed" and copied.get("id") == 401:
            raise AppServerError("transport closed", category="process_crash")
        with self._condition:
            self.sent.append(copied)
            if "id" in copied:
                self._responses[copied["id"]] = copied
            self._condition.notify_all()
        if (
            self.scenario == "early_successor"
            and copied.get("id") == 501
            and not self._early_successor_sent
        ):
            self._early_successor_sent = True
            self._dispatch_notification(
                "item/completed",
                {
                    "threadId": "thread-wor266-chat",
                    "turnId": "turn-wor266-chat",
                    "item": {
                        "id": "msg-early-post-image",
                        "type": "agentMessage",
                        "status": "completed",
                        "text": "The exact captured pixels are visible.",
                    },
                },
            )

    def _wait_response(self, request_id):
        deadline = time.monotonic() + 10.0
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"orphaned provider call {request_id}")
                self._condition.wait(remaining)
            return self._responses[request_id]

    def _dynamic_call(self, thread_id, request_id, call_id, tool, arguments):
        item = {
            "id": call_id,
            "type": "dynamicToolCall",
            "tool": tool,
            "arguments": dict(arguments),
        }
        common = {
            "threadId": thread_id,
            "turnId": "turn-wor266-chat",
        }
        self._dispatch_notification("item/started", {**common, "item": item})
        params = {
            **common,
            "callId": call_id,
            "tool": tool,
            "arguments": dict(arguments),
        }
        self.requests.append((request_id, call_id, tool, dict(arguments)))
        self._handle_server_request({
            "id": request_id,
            "method": "item/tool/call",
            "params": params,
        })
        response = self._wait_response(request_id)
        self._dispatch_notification(
            "item/completed",
            {**common, "item": {**item, "status": "completed"}},
        )
        return response

    def _finish(self, thread_id, text, *, status="completed"):
        common = {
            "threadId": thread_id,
            "turnId": "turn-wor266-chat",
        }
        if text:
            self._dispatch_notification(
                "item/agentMessage/delta",
                {**common, "delta": text},
            )
        self._dispatch_notification(
            "turn/completed",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-wor266-chat", "status": status},
            },
        )

    def _finish_completed_item(
        self,
        thread_id,
        text,
        *,
        complete_turn_first=False,
    ):
        common = {
            "threadId": thread_id,
            "turnId": "turn-wor266-chat",
        }
        item = {
            "id": "msg-post-image",
            "type": "agentMessage",
            "status": "completed",
            "text": text,
        }
        self._dispatch_notification(
            "item/started",
            {**common, "item": {**item, "status": "inProgress"}},
        )
        if complete_turn_first:
            self._dispatch_notification(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turn": {
                        "id": "turn-wor266-chat",
                        "status": "completed",
                    },
                },
            )
        self._dispatch_notification(
            "item/completed", {**common, "item": item},
        )
        if not complete_turn_first:
            self._dispatch_notification(
                "turn/completed",
                {
                    "threadId": thread_id,
                    "turn": {
                        "id": "turn-wor266-chat",
                        "status": "completed",
                    },
                },
            )

    def _simulate(self, thread_id):
        self._dispatch_notification(
            "turn/started",
            {
                "threadId": thread_id,
                "turn": {"id": "turn-wor266-chat"},
            },
        )
        if self.scenario == "exact_transport_duplicate":
            arguments = {"target": "msi center"}
            self._dynamic_call(
                thread_id, 601, "call-exact-duplicate",
                "inspect_desktop_surfaces", arguments,
            )
            self._dynamic_call(
                thread_id, 602, "call-exact-duplicate",
                "inspect_desktop_surfaces", arguments,
            )
            self._finish(thread_id, "The same correlated read was answered once.")
            return
        if self.scenario == "response_transport_failed":
            arguments = {"target": "msi center"}
            self._dispatch_notification("item/started", {
                "threadId": thread_id,
                "turnId": "turn-wor266-chat",
                "item": {
                    "id": "call-transport",
                    "type": "dynamicToolCall",
                    "tool": "inspect_desktop_surfaces",
                    "arguments": arguments,
                },
            })
            self._handle_server_request({
                "id": 401,
                "method": "item/tool/call",
                "params": {
                    "threadId": thread_id,
                    "turnId": "turn-wor266-chat",
                    "callId": "call-transport",
                    "tool": "inspect_desktop_surfaces",
                    "arguments": arguments,
                },
            })
            return
        if self.scenario in {
            "assistant_item", "delayed_output", "reasoning_then_assistant",
            "tool_after_image", "duplicate_visual", "missing_visual_output",
            "early_successor",
            "rerouted_image",
            "alternating_cycle", "transient_visual_failure",
            "bridge_failed",
        }:
            self._dynamic_call(
                thread_id, 500, "call-inspect", "inspect_desktop_surfaces",
                {"target": "msi center"},
            )
            self._dynamic_call(
                thread_id, 501, "call-vision", "request_visual_observation",
                {
                    "surface_ref": "dsr_msi",
                    "reason": "custom_rendered",
                    "target": "msi center",
                    "expected_window_generation": "win_msi",
                },
            )
            if self.scenario == "tool_after_image":
                self._dynamic_call(
                    thread_id, 502, "call-after-image",
                    "inspect_desktop_surfaces", {"target": "msi center"},
                )
                self._finish(
                    thread_id,
                    "MSI Center utilise un fond noir et gris très foncé.",
                )
                return
            if self.scenario == "alternating_cycle":
                self._dynamic_call(
                    thread_id, 502, "call-inspect-after-vision",
                    "inspect_desktop_surfaces", {"target": "msi center"},
                )
                self._dynamic_call(
                    thread_id, 503, "call-vision-after-inspect",
                    "request_visual_observation",
                    {
                        "surface_ref": "dsr_msi",
                        "reason": "custom_rendered",
                        "target": "msi center",
                        "expected_window_generation": "win_msi",
                    },
                )
                self._finish(thread_id, "The bounded visual read is complete.")
                return
            if self.scenario == "transient_visual_failure":
                for request_id, call_id in (
                    (502, "call-vision-retry"),
                    (503, "call-vision-third"),
                ):
                    self._dynamic_call(
                        thread_id, request_id, call_id,
                        "request_visual_observation",
                        {
                            "surface_ref": "dsr_msi",
                            "reason": "custom_rendered",
                            "target": "msi center",
                            "expected_window_generation": "win_msi",
                        },
                    )
                self._finish(
                    thread_id,
                    "The visual capture could not progress safely.",
                )
                return
            if self.scenario == "early_successor":
                self._finish(thread_id, "")
                return
            if self.scenario == "rerouted_image":
                self._dispatch_notification(
                    "model/rerouted",
                    {
                        "threadId": thread_id,
                        "turnId": "turn-wor266-chat",
                        "toModel": "gpt-5.6-terra",
                    },
                )
                self._finish_completed_item(
                    thread_id,
                    "This output came from a different model.",
                )
                return
            if self.scenario == "duplicate_visual":
                self._dynamic_call(
                    thread_id, 503, "call-vision-repeat",
                    "request_visual_observation",
                    {
                        "surface_ref": "dsr_msi",
                        "reason": "custom_rendered",
                        "target": "msi center",
                        "expected_window_generation": "win_msi",
                    },
                )
                self._finish(
                    thread_id,
                    "MSI Center utilise un thème sombre noir et gris.",
                )
                return
            if self.scenario in {
                "reasoning_then_assistant", "missing_visual_output",
            }:
                correlation = {
                    "threadId": thread_id,
                    "turnId": "turn-wor266-chat",
                    "itemId": "reasoning-post-image",
                    "summaryIndex": 0,
                }
                self._dispatch_notification(
                    "item/started",
                    {
                        "threadId": thread_id,
                        "turnId": "turn-wor266-chat",
                        "item": {
                            "id": "reasoning-post-image",
                            "type": "reasoning",
                            "status": "inProgress",
                        },
                    },
                )
                self._dispatch_notification(
                    "item/reasoning/summaryPartAdded",
                    {
                        **correlation,
                        "part": {"type": "summary_text", "text": ""},
                    },
                )
                self._dispatch_notification(
                    "item/reasoning/summaryTextDelta",
                    {**correlation, "delta": "Noticing no visible output"},
                )
                self._dispatch_notification(
                    "item/completed",
                    {
                        "threadId": thread_id,
                        "turnId": "turn-wor266-chat",
                        "item": {
                            "id": "reasoning-post-image",
                            "type": "reasoning",
                            "status": "completed",
                        },
                    },
                )
                if self.scenario == "missing_visual_output":
                    self._finish(thread_id, "")
                    return
            self._finish_completed_item(
                thread_id,
                "MSI Center utilise un fond noir et gris très foncé.",
                complete_turn_first=self.scenario == "delayed_output",
            )
            return
        if self.scenario in {"success", "success_failed"}:
            inspect_arguments = {"target": "msi center"}
            self._dynamic_call(
                thread_id, 101, "call-inspect", "inspect_desktop_surfaces",
                inspect_arguments,
            )
            self._dynamic_call(
                thread_id, 102, "call-inspect", "inspect_desktop_surfaces",
                inspect_arguments,
            )
            self._dynamic_call(
                thread_id, 103, "call-vision", "request_visual_observation",
                {
                    "surface_ref": "dsr_msi",
                    "reason": "custom_rendered",
                    "target": "msi center",
                    "expected_window_generation": "win_msi",
                },
            )
            self._finish(
                thread_id,
                (
                    "MSI Center affiche sa page principale et ses réglages système."
                    if self.scenario == "success" else ""
                ),
                status=("completed" if self.scenario == "success" else "failed"),
            )
            return
        if self.scenario == "uia_only":
            self._dynamic_call(
                thread_id, 111, "call-uia-only", "inspect_desktop_surfaces",
                {"target": "msi center"},
            )
            self._finish(
                thread_id,
                "Les éléments accessibles indiquent MSI Center uniquement.",
            )
            return
        if self.scenario == "cpu":
            self._finish(thread_id, "CPU usage is exactly 42%.")
            return
        if self.scenario == "wrong_call_id":
            common = {
                "threadId": thread_id,
                "turnId": "turn-wor266-chat",
            }
            arguments = {"target": "msi center"}
            self._dispatch_notification("item/started", {
                **common,
                "item": {
                    "id": "call-expected",
                    "type": "dynamicToolCall",
                    "tool": "inspect_desktop_surfaces",
                    "arguments": arguments,
                },
            })
            self.requests.append((201, "call-wrong", "inspect_desktop_surfaces", arguments))
            self._handle_server_request({
                "id": 201,
                "method": "item/tool/call",
                "params": {
                    **common,
                    "callId": "call-wrong",
                    "tool": "inspect_desktop_surfaces",
                    "arguments": arguments,
                },
            })
            self._wait_response(201)
            self._finish(thread_id, "L’identifiant de réponse a été rejeté.")
            return
        response = self._dynamic_call(
            thread_id,
            301,
            "call-unavailable",
            "deleted_visual_adapter",
            {"target": "msi center"},
        )
        assert response["result"]["success"] is False
        self._finish(
            thread_id,
            "",
            status=("failed" if self.scenario == "unavailable_failed" else "completed"),
        )


def _bound_registry(inspect_dispatcher, visual_dispatcher):
    current = tool_contracts.get_static_registry()
    dispatchers = {
        spec.dispatcher_name: spec.dispatcher
        for spec in current.values()
    }
    dispatchers["tool_inspect_desktop_surfaces"] = inspect_dispatcher
    dispatchers["tool_request_visual_observation"] = visual_dispatcher
    return tool_contracts.STATIC_TOOL_REGISTRY.bind(dispatchers)


def _inspect_result():
    observed_at = time.time()
    scene = {
        "application": "msi center",
        "requested_application": "msi center",
        "window_surface_ref": "dsr_msi",
        "window_generation": "win_msi",
        "structural_epoch": 7,
        "target_resolution": "exact_target",
        "target_foreground": True,
        "target_minimized": False,
        "accessibility_quality": "partial",
        "observed_at": observed_at,
        "surfaces": [],
    }
    return {
        "text": json.dumps({"scene": scene}, sort_keys=True),
        "scene": scene,
        "receipt": {
            "outcome": "observed",
            "reason_code": "semantic_desktop_surface_observed",
            "window_surface_ref": "dsr_msi",
            "observed_at": observed_at,
        },
    }


def _visual_result():
    artifact = create_image_artifact(
        PNG,
        "image/png",
        ImageSourceType.DESKTOP_CAPTURE,
        surface_id="dsr_msi",
        generation="win_msi",
        application_identity="msi center",
        capture_method="fixture_exact_window",
        source_pixel_format="RGB8",
        pixel_format_conversion="NONE",
        alpha_handling="OPAQUE_RGB",
        source_color_space="SRGB",
        color_space_conversion="NONE",
    )
    image_ref = register_image_artifact(
        artifact,
        owner_id=turn_scoped_media_owner(
            "conv-wor266-chat", "turn-wor266-chat",
        ),
    )
    return {
        "text": json.dumps({"contract": "same active model image"}),
        "image_ref": image_ref,
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "not_dispatched",
            "outcome": "observed",
            "reason_code": "ACTIVE_MODEL_IMAGE_PART_BUILT",
            "surface_ref": "dsr_msi",
            "window_surface_ref": "dsr_msi",
            "application": "msi center",
            "requested_application": "msi center",
            "window_generation": "win_msi",
            "frame_id": "frame_msi_full",
            "image_id": artifact.image_id,
            "image_sha256": artifact.content_hash,
            "image_mime": artifact.mime_type,
            "image_width": artifact.width,
            "image_height": artifact.height,
            "exact_surface_pixels": True,
            "content_coverage": "full_window",
            "dimensions": {"width": artifact.width, "height": artifact.height},
            "refine_count": 0,
            "provider_id": "chatgpt_account",
            "model_id": MODEL,
            "image_part_count": 1,
        },
        "diagnostics": {
            "image_input_available": True,
            "image_input_sent": False,
            "provider_id": "chatgpt_account",
            "model_id": MODEL,
        },
    }


def _transient_visual_failure_result():
    return {
        "text": "ERROR: exact capture device failed safely.",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "not_dispatched",
            "outcome": "confirmed_failure_before_effect",
            "reason_code": "CAPTURE_DEVICE_FAILED",
            "surface_ref": "dsr_msi",
            "window_surface_ref": "dsr_msi",
            "application": "msi center",
            "window_generation": "win_msi",
            "provider_id": "chatgpt_account",
            "model_id": MODEL,
        },
    }


def _stale_visual_failure_result():
    return {
        "text": "ERROR: the requested desktop target is stale.",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "not_dispatched",
            "outcome": "confirmed_failure_before_effect",
            "reason_code": "surface_stale",
            "surface_ref": "dsr_msi",
            "window_surface_ref": "dsr_msi",
            "application": "msi center",
            "window_generation": "win_msi",
            "provider_id": "chatgpt_account",
            "model_id": MODEL,
        },
    }


def _record_unknown_visual_attempt(store):
    arguments = {
        "surface_ref": "dsr_msi",
        "reason": "custom_rendered",
        "target": "msi center",
        "expected_window_generation": "win_msi",
    }
    row = store.create_attempt(
        conversation_id="conv-wor266-chat",
        turn_id="turn-wor266-chat",
        request_id="request-wor266-chat",
        tool_call_id="call-visual-unknown-prior",
        tool_name="request_visual_observation",
        provider_id="chatgpt_account",
        model_id=MODEL,
        turn_context={},
        argument_fingerprint=store.fingerprint_arguments(arguments),
        side_effect="read_only",
        idempotence="idempotent",
        retry_policy="never_after_dispatch",
        owner_scope="chat",
        provider_references={
            "tool_call_id": "call-visual-unknown-prior",
            "provider_native_call_id": "call-visual-unknown-prior",
        },
    )
    row = store.mark_accepted(row["tool_attempt_id"])
    store.mark_running(row["tool_attempt_id"])
    store.record_terminal_result(
        row["tool_attempt_id"],
        status=ToolAttemptStatus.UNKNOWN_AFTER_START,
        terminal_outcome="outcome_unknown",
        dispatch_state="effect_unknown",
        reason_code="synthetic_visual_observation_unknown",
    )
    return row["tool_attempt_id"]


def _run_chat(
    server,
    inspect_dispatcher,
    visual_dispatcher,
    *,
    request=REQUEST,
    strategy=None,
):
    provider = ChatGPTAccountProvider(client=server)
    chat = provider.chat_client("conv-wor266-chat")
    chat.set_tool_attempt_request_id("request-wor266-chat")
    chat.set_tool_attempt_turn_id("turn-wor266-chat")
    registry = _bound_registry(inspect_dispatcher, visual_dispatcher)
    model_token = desktop_scene.bind_effective_model(
        "chatgpt_account",
        MODEL,
        owner_id="conv-wor266-chat",
        turn_id="turn-wor266-chat",
    )
    try:
        with (
            patch.dict(
                TOOL_MAP,
                {
                    "inspect_desktop_surfaces": inspect_dispatcher,
                    "request_visual_observation": visual_dispatcher,
                },
            ),
            patch.object(tool_contracts, "_bound_static_registry", registry),
            patch(
                "osia.model_capabilities._exact_account_metadata",
                return_value={
                    "id": MODEL,
                    "provider": "chatgpt_account",
                    "inputModalities": ["text", "image"],
                },
            ),
            execution_strategy_scope(
                request,
                ("desktop_click", "inspect_desktop_surfaces", "request_visual_observation"),
                strategy=strategy,
            ),
        ):
            response = chat.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": request}],
            )
    finally:
        desktop_scene.reset_effective_model(model_token)
    events = chat.consume_capability_events()
    server._last_events = events
    server.last_invocation_metadata = chat.consume_invocation_metadata()
    return response, events


def _response_payload(message):
    text_item = next(
        item for item in message["result"]["contentItems"]
        if item["type"] == "inputText"
    )
    return json.loads(text_item["text"])


def test_dynamic_visual_call_closes_once_and_continues_on_same_model():
    server = _UnifiedChatAppServer("success")
    inspect_calls = []
    visual_calls = []

    response, events = _run_chat(
        server,
        lambda **kwargs: inspect_calls.append(kwargs) or _inspect_result(),
        lambda **kwargs: visual_calls.append(kwargs) or _visual_result(),
    )

    names = [item["name"] for item in server.advertised_tools]
    assert names.count("inspect_desktop_surfaces") == 1
    # The legacy Vision V3 name stays dormant; V4 visual frontiers are covered
    # by the route and cutover tests. This transcript still probes the App
    # Server callback directly with its typed compatibility tool contract.
    assert names.count("request_visual_observation") == 0
    assert server.turn_models == [MODEL]
    assert response.choices[0].message.content.startswith("MSI Center affiche")
    assert inspect_calls == [{"target": "msi center"}]
    assert len(visual_calls) == 1
    assert [message["id"] for message in server.sent] == [101, 102, 103]
    assert server.sent[0]["result"] == server.sent[1]["result"]
    assert _response_payload(server.sent[0])["call_id"] == "call-inspect"
    visual_payload = _response_payload(server.sent[2])
    assert visual_payload["call_id"] == "call-vision"
    assert visual_payload["reason_code"] == "IMAGE_INPUT_SENT"
    image_items = [
        item for item in server.sent[2]["result"]["contentItems"]
        if item["type"] == "inputImage"
    ]
    assert len(image_items) == 1
    assert image_items[0]["imageUrl"].startswith("data:image/png;base64,")
    continuation_notes = [
        item["text"] for item in server.sent[2]["result"]["contentItems"]
        if item["type"] == "inputText"
    ]
    assert len(continuation_notes) == 2
    assert "exact fresh target image" in continuation_notes[1]
    assert "request_visual_observation again" in continuation_notes[1]
    decoded = decode_image_data_url(image_items[0]["imageUrl"])
    assert decoded.data == PNG
    assert decoded.mime == "image/png"
    assert (decoded.width, decoded.height) == (1, 1)
    assert decoded.content_hash == visual_payload["result"]["receipt"][
        "image_sha256"
    ]
    assert len(server.steers) == 1
    assert [item["type"] for item in server.steers[0]["input"]] == [
        "text", "localImage",
    ]
    assert server.steers[0]["input"][1]["detail"] == "high"
    assert all(event.source == "provider_dynamic" for event in events)
    assert any(
        event.reason_code == "DYNAMIC_IMAGE_COMPATIBILITY_BRIDGED"
        for event in events
    )
    assert not any(event.outcome == "outcome_unknown" for event in events)
    assert not any("vision" in model.lower() for model in server.turn_models)


def test_unified_chat_unavailable_capability_returns_typed_terminal_result():
    server = _UnifiedChatAppServer("unavailable")
    response, events = _run_chat(server, lambda **_kwargs: _inspect_result(), lambda **_kwargs: _visual_result())

    payload = _response_payload(server.sent[0])
    assert payload["call_id"] == "call-unavailable"
    assert payload["reason_code"] == "CAPABILITY_UNAVAILABLE"
    assert server.sent[0]["result"]["success"] is False
    assert response.choices[0].message.content == ""
    assert events[-1].reason_code == "CAPABILITY_UNAVAILABLE"
    assert events[-1].outcome == "failed_before_effect"
    assert not any(event.outcome == "outcome_unknown" for event in events)


def test_codex_0144_bridge_failure_cannot_be_projected_as_visual_success():
    server = _UnifiedChatAppServer("bridge_failed")
    with pytest.raises(AccountProviderError) as caught:
        _run_chat(
            server,
            lambda **_kwargs: _inspect_result(),
            lambda **_kwargs: _visual_result(),
        )

    visual_response = next(
        item for item in server.sent if item.get("id") == 501
    )
    payload = _response_payload(visual_response)
    assert visual_response["result"]["success"] is False
    assert payload["reason_code"] == "IMAGE_DELIVERY_FAILED"
    assert payload["detail_reason_code"] == (
        "DYNAMIC_IMAGE_COMPATIBILITY_BRIDGE_FAILED"
    )
    assert not any(
        item.get("type") == "inputImage"
        for item in visual_response["result"]["contentItems"]
    )
    assert caught.value.category == "IMAGE_DELIVERY_FAILED"


def test_dynamic_image_ref_requires_complete_receipt_identity():
    server = _UnifiedChatAppServer("success")

    def incomplete_visual(**_kwargs):
        result = _visual_result()
        result["receipt"].pop("image_sha256")
        return result

    _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        incomplete_visual,
    )
    response = server.sent[-1]
    payload = _response_payload(response)
    assert payload["reason_code"] == "IMAGE_REF_METADATA_MISMATCH"
    assert response["result"]["success"] is False
    assert not any(
        item["type"] == "inputImage"
        for item in response["result"]["contentItems"]
    )


def test_new_call_id_is_not_rejected_for_matching_prior_arguments():
    server = _UnifiedChatAppServer("wrong_call_id")
    inspect_calls = []
    response, events = _run_chat(
        server,
        lambda **kwargs: inspect_calls.append(kwargs) or _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )

    assert inspect_calls == [{"target": "msi center"}]
    assert response.choices[0].message.content.startswith("L’identifiant")
    assert _response_payload(server.sent[0])["reason_code"] == (
        "semantic_desktop_surface_observed"
    )
    row = server._test_attempt_store.get_attempt(events[-1].tool_attempt_id)
    assert row["tool_call_id"] == "call-wrong"
    assert not any(event.outcome == "outcome_unknown" for event in events)


def test_first_typed_tool_failure_survives_downstream_provider_failure():
    server = _UnifiedChatAppServer("unavailable_failed")
    with pytest.raises(AccountProviderError) as captured:
        _run_chat(server, lambda **_kwargs: _inspect_result(), lambda **_kwargs: _visual_result())

    assert captured.value.category == "CAPABILITY_UNAVAILABLE"
    assert str(captured.value) == "ERROR: CAPABILITY_UNAVAILABLE"
    assert "provider response failed" not in str(captured.value).lower()


def test_successful_tool_response_then_failed_continuation_is_typed():
    server = _UnifiedChatAppServer("success_failed")
    with pytest.raises(AccountProviderError) as captured:
        _run_chat(server, lambda **_kwargs: _inspect_result(), lambda **_kwargs: _visual_result())

    assert captured.value.category == "PROVIDER_CONTINUATION_REJECTED"
    assert str(captured.value) == "ERROR: PROVIDER_CONTINUATION_REJECTED"
    assert _response_payload(server.sent[-1])["reason_code"] == "IMAGE_INPUT_SENT"


def test_missing_tool_response_transport_is_typed_and_never_replayed():
    server = _UnifiedChatAppServer("response_transport_failed")
    inspect_calls = []
    with pytest.raises(AccountProviderError) as captured:
        _run_chat(
            server,
            lambda **kwargs: inspect_calls.append(kwargs) or _inspect_result(),
            lambda **_kwargs: _visual_result(),
        )

    assert inspect_calls == [{"target": "msi center"}]
    assert captured.value.category == "TOOL_RESPONSE_MISSING"
    assert str(captured.value) == "ERROR: TOOL_RESPONSE_MISSING"


def test_terminal_projection_preserves_provider_seam_failure_code():
    agent = object.__new__(OSIAAgent)
    agent.lang = "fr"
    text = agent._terminal_final_text(
        "blocked_verified",
        "",
        snapshot={
            "strategy_snapshot": {
                "mission": {
                    "action_nodes": [{
                        "source_clause": REQUEST,
                        "required_postconditions": ["visible state observed"],
                    }],
                },
            },
            "typed_evidence": [{
                "reason_code": "CAPABILITY_UNAVAILABLE",
            }],
        },
    )
    assert "CAPABILITY_UNAVAILABLE" in text
    assert "fournisseur de réponse a échoué" not in text


def test_dynamic_observation_is_truthful_progress_and_exact_typed_evidence():
    strategy = build_execution_strategy(
        REQUEST,
        ("desktop_click", "inspect_desktop_surfaces", "request_visual_observation"),
    )
    postcondition = strategy.required_postconditions[0]
    event = CapabilityEvent(
        source="provider_dynamic",
        identity="dynamicToolCall",
        phase="completed",
        outcome="success_verified",
        item_id="call-vision",
        tool_attempt_id="call-vision",
        tool_name="request_visual_observation",
        arguments={"surface_ref": "dsr_msi", "reason": "custom_rendered"},
        semantic_postcondition=postcondition,
        reason_code="IMAGE_INPUT_SENT",
    )
    agent = object.__new__(OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = MODEL
    agent.client = SimpleNamespace(consume_capability_events=lambda: (event,))
    agent._terminality_controller = Mock()
    agent._terminality_strategy = strategy
    agent._unfinished_execution = None
    agent._tool_attempt_metadata = None
    traces = []
    agent._emit_execution_trace = (
        lambda kind, display, **kwargs: traces.append((kind, display, kwargs))
    )

    with execution_strategy_scope(REQUEST, strategy=strategy):
        summary = agent._consume_provider_capability_events()

    assert summary["accepted"] is True
    assert summary["provider_terminal"] is True
    assert summary["unknown"] is False
    assert postcondition not in strategy.proven_postconditions
    assert any(
        kind == "result" and display == "execution.result.observed"
        for kind, display, _kwargs in traces
    )
    agent._terminality_controller.record_typed_evidence.assert_called_once()
    typed_call = agent._terminality_controller.record_typed_evidence.call_args
    assert typed_call.kwargs["reason_code"] == "IMAGE_INPUT_SENT"
    assert typed_call.kwargs["postcondition"] == ""


def test_unified_chat_visual_modality_needs_image_and_correlated_answer():
    strategy = build_execution_strategy(
        REQUEST,
        ("desktop_click", "inspect_desktop_surfaces", "request_visual_observation"),
    )
    contract = goal_contract_from_semantic_mission(strategy.mission)
    assert contract.nodes[0].evidence_requirement == "visual"

    server = _UnifiedChatAppServer("success")
    response, events = _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
        strategy=strategy,
    )
    postcondition = strategy.required_postconditions[0]
    assert postcondition not in strategy.proven_postconditions
    consumed = next(
        event for event in events
        if event.reason_code == "IMAGE_INPUT_CONSUMED"
    )
    assert strategy.mark_visual_input_consumed(
        provider="chatgpt_account",
        model=MODEL,
        tool_call_id=consumed.item_id,
    )
    proven = strategy.prove_visual_answer(
        response.choices[0].message.content,
        evidence="provider_answer:unified-chat",
        provider="chatgpt_account",
        model=MODEL,
    )
    assert proven == (postcondition,)
    assert "visual_input:frame_msi_full" in strategy.proven_postconditions[
        postcondition
    ]


def test_unified_chat_uia_only_cannot_impersonate_visual_perception():
    strategy = build_execution_strategy(
        REQUEST,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    server = _UnifiedChatAppServer("uia_only")
    response, _events = _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
        strategy=strategy,
    )
    assert strategy.prove_visual_answer(
        response.choices[0].message.content,
        evidence="provider_answer:uia-only",
        provider="chatgpt_account",
        model=MODEL,
    ) == ()
    assert not strategy.proven_postconditions


def test_unified_chat_cpu_answer_needs_no_image():
    request = "What is the exact CPU usage?"
    strategy = build_execution_strategy(
        request,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    contract = goal_contract_from_semantic_mission(strategy.mission)
    assert contract.nodes[0].evidence_requirement == "structured"
    server = _UnifiedChatAppServer("cpu")
    response, _events = _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
        request=request,
        strategy=strategy,
    )
    assert response.choices[0].message.content == "CPU usage is exactly 42%."
    assert not strategy._latest_desktop_observation.get("visual_evidence_history")


def test_visual_read_goal_contract_never_creates_mutation_uncertainty():
    strategy = build_execution_strategy(
        REQUEST,
        ("desktop_click", "inspect_desktop_surfaces", "request_visual_observation"),
    )
    contract = goal_contract_from_semantic_mission(strategy.mission)

    assert len(contract.nodes) == 1
    assert contract.nodes[0].read_only is True
    assert contract.nodes[0].mutation is False
    assert contract.nodes[0].side_effect_class == "read_only"


def test_post_image_01_visual_capability_is_called():
    server = _UnifiedChatAppServer("assistant_item")
    visual_calls = []
    _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **kwargs: visual_calls.append(kwargs) or _visual_result(),
    )
    assert len(visual_calls) == 1


def test_post_image_02_image_is_attached_to_the_same_model():
    server = _UnifiedChatAppServer("assistant_item")
    _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    response = server.sent[1]
    assert server.turn_models == [MODEL]
    assert sum(
        item["type"] == "inputImage"
        for item in response["result"]["contentItems"]
    ) == 1
    payload = _response_payload(response)
    image_id = payload["result"]["receipt"]["image_id"]
    identity = image_inputs._delivery_identity(
        image_id=image_id,
        provider="chatgpt_account",
        model=MODEL,
        conversation_id="conv-wor266-chat",
        request_id="request-wor266-chat",
        turn_id="turn-wor266-chat",
        tool_call_id="call-vision",
        rpc_id="501",
        envelope_type="inputImage",
        transport_id="generation:0:thread:thread-wor266-chat",
    )
    delivery = image_inputs.media_delivery_record(
        "delivery_" + identity[:32]
    )
    assert delivery is not None and delivery.state == "ACKNOWLEDGED"
    bridge_identity = image_inputs._delivery_identity(
        image_id=payload["result"]["receipt"]["image_id"],
        provider="chatgpt_account",
        model=MODEL,
        conversation_id="conv-wor266-chat",
        request_id="request-wor266-chat",
        turn_id="turn-wor266-chat",
        tool_call_id="call-vision",
        rpc_id="turn-steer:501",
        envelope_type="turnSteerImage",
        transport_id=(
            "generation:0:thread:thread-wor266-chat:"
            "codex-0.144-dynamic-image-bridge"
        ),
    )
    bridge_delivery = image_inputs.media_delivery_record(
        "delivery_" + bridge_identity[:32]
    )
    assert (
        bridge_delivery is not None
        and bridge_delivery.state == "ACKNOWLEDGED"
    )


def test_post_image_03_provider_returns_assistant_text_after_image():
    server = _UnifiedChatAppServer("assistant_item")
    response, events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert response.choices[0].message.content.startswith("MSI Center utilise")
    assert any(event.reason_code == "IMAGE_INPUT_CONSUMED" for event in events)


def test_post_image_04_completed_assistant_item_is_extracted():
    server = _UnifiedChatAppServer("assistant_item")
    response, _events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    metadata = server.last_invocation_metadata
    assert response.choices[0].message.content.endswith("très foncé.")
    assert metadata["final_output_item_id"] == "msg-post-image"
    assert any(
        item["item_type"] == "agentMessage"
        and item["phase"] == "completed"
        and item["text_present"] is True
        for item in metadata["response_items"]
    )


def test_post_image_05_provider_can_call_another_tool_after_image():
    server = _UnifiedChatAppServer("tool_after_image")
    _response, events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert [request[2] for request in server.requests] == [
        "inspect_desktop_surfaces", "request_visual_observation",
        "inspect_desktop_surfaces",
    ]
    after_image = next(
        event for event in events
        if event.item_id == "call-after-image"
        and event.phase == "completed"
    )
    row = server._test_attempt_store.get_attempt(after_image.tool_attempt_id)
    assert row["tool_call_id"] == "call-after-image"


def test_post_image_06_successor_tool_correlates_the_continuation():
    server = _UnifiedChatAppServer("tool_after_image")
    response, events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    consumed = [
        event for event in events
        if event.reason_code == "IMAGE_INPUT_CONSUMED"
    ]
    assert [(event.item_id, event.outcome) for event in consumed] == [
        ("call-vision", "success_verified"),
    ]
    assert consumed[0].tool_attempt_id != consumed[0].item_id
    assert response.choices[0].message.content


def test_post_image_07_reasoning_summary_is_not_the_final_output():
    server = _UnifiedChatAppServer("reasoning_then_assistant")
    response, _events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    content = response.choices[0].message.content
    assert content.startswith("MSI Center utilise")
    assert "Noticing no visible output" not in content


def test_post_image_08_delayed_output_item_is_awaited():
    server = _UnifiedChatAppServer("delayed_output")
    response, _events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert response.choices[0].message.content.endswith("très foncé.")
    assert len(server.steers) == 1


def test_post_image_09_turn_done_before_item_done_is_correlated():
    server = _UnifiedChatAppServer("delayed_output")
    _response, _events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert server.last_invocation_metadata["native_status"] == "completed"
    assert server.last_invocation_metadata[
        "final_output_item_id"
    ] == "msg-post-image"


def test_post_image_09b_synchronous_successor_during_send_is_correlated_once():
    server = _UnifiedChatAppServer("early_successor")
    response, events = _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    consumed = [
        event for event in events
        if event.reason_code == "IMAGE_INPUT_CONSUMED"
    ]
    assert response.choices[0].message.content == (
        "The exact captured pixels are visible."
    )
    assert len(consumed) == 1
    visual_attempt = server._test_attempt_store.get_attempt(
        consumed[0].tool_attempt_id,
    )
    assert visual_attempt is not None
    assert visual_attempt["tool_attempt_id"] != "call-vision"
    assert visual_attempt["tool_call_id"] == "call-vision"
    assert visual_attempt["provider_references"][
        "provider_native_call_id"
    ] == "call-vision"
    assert visual_attempt["status"] == "succeeded"
    assert visual_attempt["action_truth"]["effect_class"] == "read_only"
    assert visual_attempt["action_truth"]["effect_truth"] == "verified"
    assert len(server.steers) == 1
    visual_response = next(
        item for item in server.sent if item.get("id") == 501
    )
    assert sum(
        item["type"] == "inputImage"
        for item in visual_response["result"]["contentItems"]
    ) == 1
    payload = _response_payload(visual_response)
    identity = image_inputs._delivery_identity(
        image_id=payload["result"]["receipt"]["image_id"],
        provider="chatgpt_account",
        model=MODEL,
        conversation_id="conv-wor266-chat",
        request_id="request-wor266-chat",
        turn_id="turn-wor266-chat",
        tool_call_id="call-vision",
        rpc_id="501",
        envelope_type="inputImage",
        transport_id="generation:0:thread:thread-wor266-chat",
    )
    delivery = image_inputs.media_delivery_record(
        "delivery_" + identity[:32]
    )
    assert delivery is not None and delivery.state == "ACKNOWLEDGED"


def test_terminal_visual_toolattempt_projects_without_a_dangling_wait(
    tmp_path, monkeypatch,
):
    server = _UnifiedChatAppServer("assistant_item")
    _response, events = _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    monkeypatch.setattr(
        "osia.tool_attempts.get_tool_attempt_store",
        lambda: server._test_attempt_store,
    )
    controller = TurnTerminalityController(
        turn_id="turn-wor266-chat",
        objective="Observe the requested desktop surface",
        execution_required=True,
        storage_dir=tmp_path / "turns",
    )
    binding = {
        "conversation_id": "conv-wor266-chat",
        "request_id": "request-wor266-chat",
        "turn_id": "turn-wor266-chat",
        "run_id": "turn-wor266-chat",
        "provider_id": "chatgpt_account",
        "model_id": MODEL,
        "effective_reasoning": "medium",
    }
    controller.set_execution_binding(binding)
    agent = object.__new__(OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = MODEL
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: events,
    )
    agent._terminality_controller = controller
    agent._terminality_strategy = None
    agent._tool_attempt_metadata = {
        **binding,
        "owner_scope": "chat",
    }
    agent._unfinished_execution = None
    agent._emit_execution_trace = lambda *_args, **_kwargs: None

    agent._consume_provider_capability_events()

    visual_events = [
        event for event in events
        if event.tool_name == "request_visual_observation"
        and event.phase == "completed"
    ]
    assert len(visual_events) == 1
    attempt_id = visual_events[0].tool_attempt_id
    row = server._test_attempt_store.get_attempt(attempt_id)
    assert row["tool_call_id"] == "call-vision"
    assert row["status"] == "succeeded"
    assert row["action_truth"]["effect_class"] == "read_only"
    assert attempt_id not in controller.snapshot()["active_attempts"]
    assert not controller.snapshot()["unknown_effects"]


def test_post_image_09c_model_reroute_fails_closed_for_same_model_vision():
    server = _UnifiedChatAppServer("rerouted_image")
    with pytest.raises(AccountProviderError) as captured:
        _run_chat(
            server,
            lambda **_kwargs: _inspect_result(),
            lambda **_kwargs: _visual_result(),
        )
    assert captured.value.category == "MODEL_IDENTITY_CHANGED"


def test_post_image_10_identical_frame_is_not_resent():
    server = _UnifiedChatAppServer("duplicate_visual")
    _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    image_counts = [
        sum(
            item["type"] == "inputImage"
            for item in response["result"]["contentItems"]
        )
        for response in server.sent
    ]
    assert image_counts == [0, 1, 0]
    assert _response_payload(server.sent[2])["reason_code"] == (
        "IMAGE_INPUT_ALREADY_SENT"
    )
    repeated = _response_payload(server.sent[2])["result"]
    assert repeated["visual_frame_id"] == "frame_msi_full"
    visual_attempts = [
        server._test_attempt_store.get_attempt(event.tool_attempt_id)
        for event in server._last_events
        if event.tool_name == "request_visual_observation"
        and event.phase == "completed"
    ]
    assert len(visual_attempts) == 2
    assert len({row["tool_attempt_id"] for row in visual_attempts}) == 2
    assert [row["tool_call_id"] for row in visual_attempts] == [
        "call-vision", "call-vision-repeat",
    ]


def test_post_image_10b_alternating_frontier_allows_a_new_visual_call_id():
    server = _UnifiedChatAppServer("alternating_cycle")
    visual_calls = []
    _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **kwargs: visual_calls.append(kwargs) or _visual_result(),
    )
    assert len(visual_calls) == 2
    assert _response_payload(server.sent[-1])["reason_code"] != "NO_PROGRESS"


def test_post_image_10c_transient_visual_failures_do_not_block_new_call_ids():
    server = _UnifiedChatAppServer("transient_visual_failure")
    visual_calls = []
    _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **kwargs: visual_calls.append(kwargs)
        or _transient_visual_failure_result(),
    )
    assert len(visual_calls) == 3
    assert _response_payload(server.sent[-1])["reason_code"] == (
        "CAPTURE_DEVICE_FAILED"
    )
    attempts = [
        server._test_attempt_store.get_attempt(event.tool_attempt_id)
        for event in server._last_events
        if event.tool_name == "request_visual_observation"
        and event.phase == "completed"
    ]
    assert len(attempts) == 3
    assert all(row["status"] == "failed_before_effect" for row in attempts)


def test_post_image_11_every_visual_call_has_one_response():
    server = _UnifiedChatAppServer("duplicate_visual")
    _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert [request[0] for request in server.requests] == [500, 501, 503]
    assert [message["id"] for message in server.sent] == [500, 501, 503]


def test_direct_visual_same_transport_call_id_is_deduped_once():
    server = _UnifiedChatAppServer("exact_transport_duplicate")
    inspect_calls = []
    _run_chat(
        server,
        lambda **kwargs: inspect_calls.append(kwargs) or _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert len(inspect_calls) == 1
    assert [request[0] for request in server.requests] == [601, 602]
    first = next(item for item in server.sent if item.get("id") == 601)
    second = next(item for item in server.sent if item.get("id") == 602)
    assert first["result"] == second["result"]
    events = [
        event for event in server._last_events
        if event.tool_name == "inspect_desktop_surfaces"
        and event.phase == "completed"
    ]
    assert len(events) == 1
    row = server._test_attempt_store.get_attempt(events[0].tool_attempt_id)
    assert row["tool_call_id"] == "call-exact-duplicate"


def test_new_visual_call_id_runs_after_terminal_outcome_unknown():
    server = _UnifiedChatAppServer("assistant_item")
    prior_attempt_id = _record_unknown_visual_attempt(
        server._test_attempt_store,
    )
    visual_calls = []
    _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **kwargs: visual_calls.append(kwargs) or _visual_result(),
    )
    assert len(visual_calls) == 1
    visual = next(
        event for event in server._last_events
        if event.tool_name == "request_visual_observation"
        and event.phase == "completed"
    )
    assert visual.tool_attempt_id != prior_attempt_id
    row = server._test_attempt_store.get_attempt(visual.tool_attempt_id)
    assert row["tool_call_id"] == "call-vision"
    assert row["status"] == "succeeded"


def test_stale_visual_request_keeps_freshness_rejection():
    server = _UnifiedChatAppServer("assistant_item")
    _run_chat(
        server,
        lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _stale_visual_failure_result(),
    )
    response = next(
        item for item in server.sent if item.get("id") == 501
    )
    assert _response_payload(response)["reason_code"] == "surface_stale"
    visual = next(
        event for event in server._last_events
        if event.tool_name == "request_visual_observation"
        and event.phase == "completed"
    )
    row = server._test_attempt_store.get_attempt(visual.tool_attempt_id)
    assert row["status"] == "failed_before_effect"
    assert row["reason_code"] == "surface_stale"


def test_post_image_12_first_typed_cause_survives_generic_failure():
    server = _UnifiedChatAppServer("unavailable_failed")
    with pytest.raises(AccountProviderError) as captured:
        _run_chat(
            server, lambda **_kwargs: _inspect_result(),
            lambda **_kwargs: _visual_result(),
        )
    assert captured.value.category == "CAPABILITY_UNAVAILABLE"
    assert "provider response failed" not in str(captured.value).lower()


def test_post_image_13_no_secondary_vision_model_is_used():
    server = _UnifiedChatAppServer("assistant_item")
    _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(),
    )
    assert server.turn_models == ["gpt-5.6-terra"]


def test_post_image_14_visual_answer_requires_a_consumed_frame():
    strategy = build_execution_strategy(
        REQUEST,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    server = _UnifiedChatAppServer("success")
    response, _events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(), strategy=strategy,
    )
    assert strategy.prove_visual_answer(
        response.choices[0].message.content,
        evidence="provider-answer:unconsumed",
        provider="chatgpt_account",
        model=MODEL,
    ) == ()
    assert strategy._latest_desktop_observation["visual_input_present"] is False
    assert not strategy._latest_desktop_observation.get("visual_answer_rejection")


def test_post_image_15_uia_only_cannot_satisfy_visual_appearance():
    strategy = build_execution_strategy(
        REQUEST,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    server = _UnifiedChatAppServer("uia_only")
    response, _events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(), strategy=strategy,
    )
    assert strategy.prove_visual_answer(
        response.choices[0].message.content,
        evidence="provider-answer:uia",
        provider="chatgpt_account",
        model=MODEL,
    ) == ()
    assert not strategy.proven_postconditions


def test_real_msi_process_identity_does_not_override_requested_application():
    strategy = build_execution_strategy(
        "tu vois quoi sur la fenetre msi center ? c'est quoi le fond de theme",
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )

    def real_identity_visual_result():
        result = _visual_result()
        artifact = create_image_artifact(
            PNG,
            "image/png",
            ImageSourceType.DESKTOP_CAPTURE,
            surface_id="dsr_msi",
            generation="win_msi",
            application_identity="dcv2",
            capture_method="fixture_exact_window",
            source_pixel_format="RGB8",
            pixel_format_conversion="NONE",
            alpha_handling="OPAQUE_RGB",
            source_color_space="SRGB",
            color_space_conversion="NONE",
        )
        result["image_ref"] = register_image_artifact(
            artifact,
            owner_id=turn_scoped_media_owner(
                "conv-wor266-chat", "turn-wor266-chat",
            ),
        )
        result["receipt"] = dict(result["receipt"])
        result["receipt"].update({
            "application": "dcv2",
            "requested_application": "msi center",
            "image_id": artifact.image_id,
            "image_sha256": artifact.content_hash,
            "image_mime": artifact.mime_type,
            "image_width": artifact.width,
            "image_height": artifact.height,
        })
        return result

    server = _UnifiedChatAppServer("assistant_item")
    response, events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: real_identity_visual_result(), strategy=strategy,
    )
    consumed = next(
        event for event in events
        if event.reason_code == "IMAGE_INPUT_CONSUMED"
    )
    assert strategy.mark_visual_input_consumed(
        provider="chatgpt_account",
        model=MODEL,
        tool_call_id=consumed.item_id,
    )
    assert strategy.prove_visual_answer(
        response.choices[0].message.content,
        evidence="provider-answer:real-msi",
        provider="chatgpt_account",
        model=MODEL,
    ) == strategy.required_postconditions


def test_agent_projects_image_consumption_before_visual_answer_proof():
    strategy = build_execution_strategy(
        REQUEST,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    server = _UnifiedChatAppServer("assistant_item")
    response, events = _run_chat(
        server, lambda **_kwargs: _inspect_result(),
        lambda **_kwargs: _visual_result(), strategy=strategy,
    )
    consumed = next(
        event for event in events
        if event.reason_code == "IMAGE_INPUT_CONSUMED"
    )
    agent = object.__new__(OSIAAgent)
    agent.provider = "chatgpt_account"
    agent.model = MODEL
    agent.client = SimpleNamespace(
        consume_capability_events=lambda: (consumed,),
    )
    agent._terminality_controller = Mock()
    agent._terminality_strategy = strategy
    agent._unfinished_execution = None
    agent._tool_attempt_metadata = None
    agent._emit_execution_trace = lambda *_args, **_kwargs: None
    with execution_strategy_scope(REQUEST, strategy=strategy):
        agent._consume_provider_capability_events()
    assert strategy._latest_desktop_observation[
        "visual_image_input_consumed"
    ] is True
    assert strategy.prove_visual_answer(
        response.choices[0].message.content,
        evidence="provider-answer:agent-projection",
        provider="chatgpt_account",
        model=MODEL,
    ) == strategy.required_postconditions


def test_missing_post_image_output_has_a_typed_first_cause():
    server = _UnifiedChatAppServer("missing_visual_output")
    with pytest.raises(AccountProviderError) as captured:
        _run_chat(
            server, lambda **_kwargs: _inspect_result(),
            lambda **_kwargs: _visual_result(),
        )
    assert captured.value.category == "MODEL_VISUAL_OUTPUT_MISSING"
    assert str(captured.value) == "ERROR: MODEL_VISUAL_OUTPUT_MISSING"
    assert captured.value.response_items
