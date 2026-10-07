"""WOR-266 Option D target, terminality, media, race and privacy proofs."""

from __future__ import annotations

import base64
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.skip(
    "Legacy WOR-266 V2/V3 suite is superseded by the canonical Chat and "
    "Computer Use V4 coverage in WOR-289/WOR-305; its local probe/audit "
    "helpers are intentionally excluded from release checkouts.",
    allow_module_level=True,
)
from PIL import Image

from osia import desktop_scene, execution_strategy as execution_strategy_module, image_inputs
from osia.chatgpt_account import app_server_client as app_server_client_module
from osia.chatgpt_account import provider as account_provider_module
from osia.agent import (
    OSIAAgent,
    _normal_visual_answer_output,
    _rehydrate_persisted_image_message,
    _relevant_image_history,
)
from osia.chatgpt_account.app_server_client import (
    AppServerClient,
    AppServerError,
    _TurnAttempt,
    redact_app_server_log_body,
)
from osia.chatgpt_account.provider import (
    ChatGPTAccountProvider,
    _native_rebuild_history,
    _public_message,
)
from osia.desktop_surface import DesktopSurfaceRef, SurfaceKind
from osia.execution_strategy import (
    build_execution_strategy,
    execution_strategy_scope,
)
from osia.goal_contract import goal_contract_from_semantic_mission
from osia.image_inputs import (
    ImageInputError,
    ImageSourceType,
    MediaDeliveryState,
    MediaEnvelopeType,
    acquire_image_lease,
    begin_media_delivery,
    cleanup_media_store,
    cleanup_owned_temp_images,
    commit_media_delivery,
    create_image_artifact,
    fail_media_delivery,
    media_delivery_record,
    mark_media_delivery_commit_unknown,
    prepare_media_delivery,
    register_image_artifact,
    release_image_lease,
    resolve_image_artifact,
    resolve_image_reference,
    turn_scoped_media_owner,
)
from osia.model_capabilities import effective_route_capabilities
from osia.tool_attempts import WAITING_TOOL_RESULT
from osia.turn_terminality import (
    TerminalState,
    TurnTerminalityController,
    WaitType,
)
from osia.wor266_live_probe import _parse_probe_json_object
from osia.visual_observation import (
    CaptureKind,
    ObservationReason,
    VisualObservationStore,
)


PHYSICAL_PHRASE = (
    "regarde la fenetre msi center dis moi ce que tu vois et decris mon le theme"
)


def _png(color: str = "navy") -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new("RGB", (48, 32), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def isolated_media_store(tmp_path, monkeypatch):
    monkeypatch.setattr(image_inputs, "CONFIG_DIR", str(tmp_path))
    with image_inputs._IMAGE_STORE_LOCK:
        image_inputs._IMAGE_STORE.clear()
        image_inputs._IMAGE_STORE_BYTES = 0
    yield tmp_path
    with image_inputs._IMAGE_STORE_LOCK:
        image_inputs._IMAGE_STORE.clear()
        image_inputs._IMAGE_STORE_BYTES = 0


def _scene(application: str, title: str, ref: str = "window"):
    window = DesktopSurfaceRef(
        ref_id=ref,
        application_id=application,
        process_ref="process-" + ref,
        window_generation="generation-" + ref,
        surface_kind=SurfaceKind.WINDOW,
        role="WindowControl",
        accessible_label=title,
    )
    return desktop_scene.DesktopScene(
        application_id=application,
        window_ref=window,
        surfaces=(window,),
        structural_epoch=1,
        scene_signature="scene-" + ref,
        accessibility_complete=True,
        observed_at=time.time(),
        host_application_id=application,
    )


@pytest.mark.parametrize(
    ("phrase", "expected"),
    (
        (PHYSICAL_PHRASE, "msi center"),
        ("regarde la fenêtre microsoft store et dis moi ce que tu vois", "microsoft store"),
        ("observe la fenetre visual studio code donne moi les details", "vscode"),
        ("sur la fenetre msi center tu vois quoi", "msi center"),
        ("inspect steam and tell me what you see", "steam"),
    ),
)
def test_target_hint_parses_punctuation_free_multilingual_phrases(phrase, expected):
    strategy = build_execution_strategy(
        phrase,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    assert strategy.goal.required_application == expected
    assert strategy.goal.required_application != "msi center dis moi"
    hint = strategy.goal.target_hint
    assert hint["normalized_value"] == expected
    assert hint["source"] in {
        "known_application_catalogue",
        "weak_natural_language_heuristic",
    }


def test_weak_target_hint_survives_in_goal_contract_without_becoming_exact():
    strategy = build_execution_strategy(
        PHYSICAL_PHRASE,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    contract = goal_contract_from_semantic_mission(strategy.mission)
    hint = contract.nodes[0].target_hint
    assert contract.nodes[0].application == "msi center"
    assert hint["source"] == "weak_natural_language_heuristic"
    assert hint["exactness"] is False
    assert 0 < hint["confidence"] < 1


def test_exact_model_target_is_not_replaced_by_weaker_strategy_hint():
    strategy = build_execution_strategy(
        PHYSICAL_PHRASE,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    with execution_strategy_scope(PHYSICAL_PHRASE, strategy=strategy):
        assert desktop_scene._requested_application("MSI Center") == "msi center"
        candidates = desktop_scene._target_candidates("dcv2")
    assert candidates[0]["value"] == "dcv2"
    assert candidates[0]["source"] == "model_tool_argument"
    assert candidates[1]["value"] == "msi center"
    assert candidates[1]["exactness"] is False


def test_dcv2_and_msi_title_are_proven_aliases_on_one_observed_scene():
    scene = _scene("dcv2", "MSI Center")
    process_rank, process_evidence = desktop_scene._scene_application_identity(
        scene, "dcv2",
    )
    title_rank, title_evidence = desktop_scene._scene_application_identity(
        scene, "MSI Center",
    )
    assert process_rank == 400 and process_evidence.startswith("executable_application")
    assert title_rank == 300 and title_evidence.startswith("exact_window_title")


def test_exact_phrase_goal_contract_and_observed_msi_scene_resolve():
    strategy = build_execution_strategy(
        PHYSICAL_PHRASE,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    contract = goal_contract_from_semantic_mission(strategy.mission)
    scene = _scene("dcv2", "MSI Center")
    selection = desktop_scene._resolve_target_scene(
        (scene,),
        "MSI Center",
        foreground_scene=scene,
    )
    assert contract.nodes[0].application == "msi center"
    assert selection.status == "exact_target"
    assert selection.target_scene is scene


def test_conflicting_unproven_targets_are_diagnostic_candidates_not_overrides():
    strategy = build_execution_strategy(
        "regarde la fenetre alpha suite donne moi les details",
        ("inspect_desktop_surfaces",),
    )
    with execution_strategy_scope("x", strategy=strategy):
        candidates = desktop_scene._target_candidates("beta suite")
    assert [item["value"] for item in candidates] == ["beta suite", "alpha suite"]
    assert candidates[0]["exactness"] is True
    assert candidates[1]["exactness"] is False


def test_target_parser_contains_no_msi_specific_hardcoding():
    source = inspect.getsource(sys.modules["osia.execution_strategy"])
    assert "msi center" not in source.casefold()
    assert "dcv2" not in source.casefold()


def _controller(tmp_path: Path, suffix: str = "turn") -> TurnTerminalityController:
    return TurnTerminalityController(
        turn_id=suffix,
        objective=PHYSICAL_PHRASE,
        required_postconditions=("visual description exists",),
        execution_required=True,
        storage_dir=tmp_path,
    )


def test_target_missing_keeps_turn_active_and_preserves_first_typed_cause(tmp_path):
    # WOR-303: a temporary target-resolution failure is never terminal while
    # safe possibilities remain; the typed cause is preserved for the next
    # canonical continuation segment.
    controller = _controller(tmp_path, "target-missing")
    strategy = build_execution_strategy(
        PHYSICAL_PHRASE,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    strategy._latest_desktop_observation = {
        "requested_application": "msi center",
        "requested_target_candidates": [strategy.goal.target_hint],
        "target_resolution": "target_missing",
        "target_conflict": False,
        "target_candidate_count": 0,
        "top_level_surfaces": [{"application": "dcv2", "title": "MSI Center"}],
        "last_capability": "inspect_desktop_surfaces",
        "last_typed_result": "semantic_target_surface_not_found",
        "evidence_revision": 1,
    }
    controller.set_strategy_snapshot(strategy.snapshot())
    decision = controller.evaluate(provider_route="chatgpt_account:gpt-5.6-sol")
    snapshot = controller.snapshot()
    assert decision.terminal_state == ""
    assert decision.action == "continue"
    assert snapshot["terminal_state"] == ""
    assert snapshot["first_causal_error"]["reason_code"] == "TARGET_MISSING"


def test_wait_types_do_not_overload_pending_tool(tmp_path):
    pending = _controller(tmp_path, "pending")
    pending.set_attempt_state("attempt-1", status="pending")
    decision = pending.evaluate()
    assert decision.action == "wait"
    assert decision.wait_type == WaitType.WAIT_PENDING_TOOL.value

    provider = _controller(tmp_path, "provider")
    assert provider.begin_invocation(
        provider_route="route", strategy_id="initial", postcondition="p",
    )
    decision = provider.evaluate()
    assert decision.wait_type == WaitType.WAIT_PROVIDER_CONTINUATION.value


def test_structural_wait_without_pending_attempt_keeps_turn_active(tmp_path):
    # WOR-303: a structural wait without a proven pending attempt is a stalled
    # cycle of the same canonical turn, never a terminal.
    controller = _controller(tmp_path, "structural")
    controller.mark_structural_wait(
        "WAIT_WITHOUT_PENDING_TOOL", evidence={"pending": False},
    )
    decision = controller.evaluate()
    assert decision.terminal_state == ""
    assert decision.action == "continue"
    snapshot = controller.snapshot()
    assert snapshot["structural_wait_reason"] == "WAIT_WITHOUT_PENDING_TOOL"


def test_agent_cannot_activate_nonexistent_pending_tool_attempt():
    agent = OSIAAgent(lang="en")
    agent.last_pending_tool_attempt_id = "attempt-does-not-exist"
    agent._pending_tool_runtime = None
    assert agent.pending_tool_attempt_record() is None
    assert agent.activate_pending_tool_attempt() is False


def test_completed_provider_candidates_are_not_classified_as_transport_failure(tmp_path):
    controller = _controller(tmp_path, "completed-provider")
    invocation = controller.begin_invocation(
        provider_route="chatgpt_account:gpt-5.6-sol",
        strategy_id="provider_initial",
        postcondition="visual description exists",
    )
    controller.record_provider_invocation_completed(
        invocation,
        native_turn_id="native-turn",
        native_status="completed",
        response_fingerprint="abc",
    )
    controller.mark_structural_wait(
        "adaptive_success_not_eligible", evidence={"same": True},
    )
    decision = controller.evaluate()
    assert "provider" not in decision.reason_code.casefold()
    snapshot = controller.snapshot()
    assert snapshot["first_causal_error"]["reason_code"] == (
        "adaptive_success_not_eligible"
    )
    assert decision.terminal_state == ""


def test_canonical_capture_protected_reason_retires_visual_frontier(tmp_path):
    controller = _controller(tmp_path, "capture-protected")
    controller.set_strategy_snapshot({
        "alternatives_still_applicable": ["inspect_desktop_surfaces"],
        "latest_desktop_observation": {
            "visual_failure_reason": "CAPTURE_PROTECTED",
        },
    })

    decision = controller.evaluate()
    snapshot = controller.snapshot()

    assert decision.terminal_state == TerminalState.BLOCKED_VERIFIED.value
    assert decision.reason_code == "CAPTURE_PROTECTED"
    assert snapshot["strategy_frontier"] == []
    assert snapshot["first_causal_error"]["reason_code"] == "CAPTURE_PROTECTED"


@pytest.mark.parametrize(
    "later_reason",
    ("adaptive_success_not_eligible", "WAIT_WITHOUT_PENDING_TOOL"),
)
def test_first_typed_failure_survives_later_generic_terminality(
    tmp_path,
    later_reason,
):
    controller = _controller(tmp_path, "first-cause-" + later_reason.lower())
    controller.record_typed_evidence(
        outcome="failed_before_effect",
        evidence={"target": "missing"},
        reason_code="TARGET_MISSING",
    )
    controller.mark_structural_wait(
        later_reason,
        evidence={"later": True},
    )

    restored = _controller(tmp_path, "first-cause-" + later_reason.lower())
    decision = restored.evaluate()
    snapshot = restored.snapshot()

    # WOR-303: the turn continues; the FIRST typed cause is what a later
    # genuine terminal would report, never the generic later marker.
    assert decision.terminal_state == ""
    assert snapshot["first_causal_error"]["reason_code"] == "TARGET_MISSING"
    agent = OSIAAgent(lang="en")
    final = agent._terminal_final_text(
        TerminalState.BLOCKED_VERIFIED.value,
        later_reason,
        snapshot=snapshot,
    )
    assert "reliably identify the requested application window" in final
    assert "cannot safely progress" not in final


def test_normal_visual_answer_output_requires_noncontrol_assistant_prose():
    assert not _normal_visual_answer_output("")
    assert not _normal_visual_answer_output("   ")
    assert not _normal_visual_answer_output(WAITING_TOOL_RESULT)
    assert _normal_visual_answer_output(
        "The window shows a dark navy interface with violet accents."
    )
    source = inspect.getsource(OSIAAgent._run_turn)
    assert "if _normal_visual_answer_output(content):" in source


def test_canonical_artifact_reference_is_owner_bound_and_metadata_verified(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png(), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    assert resolve_image_reference(reference, owner_id="conversation-a") is artifact
    with pytest.raises(ImageInputError) as owner_error:
        resolve_image_reference(reference, owner_id="conversation-b")
    assert owner_error.value.reason_code == "IMAGE_OWNER_MISMATCH"
    mismatched = dict(reference, width=artifact.width + 1)
    with pytest.raises(ImageInputError) as metadata_error:
        resolve_image_reference(mismatched, owner_id="conversation-a")
    assert metadata_error.value.reason_code == "IMAGE_REF_METADATA_MISMATCH"


def test_expired_reference_is_typed_and_not_silently_text_only(
    isolated_media_store,
):
    now = time.time()
    artifact = create_image_artifact(
        _png("red"),
        "image/png",
        ImageSourceType.USER_UPLOAD,
        created_at=now - 10,
        expires_at=now - 1,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    with pytest.raises(ImageInputError) as error:
        resolve_image_reference(reference, owner_id="conversation-a")
    assert error.value.reason_code == "IMAGE_REF_EXPIRED"


def test_desktop_frame_restart_semantics_are_explicitly_ephemeral(
    isolated_media_store,
    monkeypatch,
):
    artifact = create_image_artifact(
        _png("teal"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    with image_inputs._IMAGE_STORE_LOCK:
        image_inputs._IMAGE_STORE.clear()
        image_inputs._IMAGE_STORE_BYTES = 0
    monkeypatch.setattr(image_inputs, "_producer_is_alive", lambda *_args: False)
    with pytest.raises(ImageInputError) as error:
        resolve_image_reference(reference, owner_id="conversation-a")
    assert error.value.reason_code == "IMAGE_REF_EXPIRED"


def test_active_lease_pins_expired_artifact_until_delivery_finishes(
    isolated_media_store,
    monkeypatch,
):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(image_inputs.time, "time", lambda: clock["now"])
    artifact = create_image_artifact(
        _png("green"),
        "image/png",
        ImageSourceType.DESKTOP_CAPTURE,
        created_at=1_000.0,
        expires_at=1_001.0,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    lease = acquire_image_lease(
        reference,
        owner_id="conversation-a",
        lease_owner_id="delivery-a",
    )
    clock["now"] = 1_002.0
    cleanup_media_store(now=clock["now"])
    assert resolve_image_reference(reference, owner_id="conversation-a").image_id == artifact.image_id
    assert release_image_lease(artifact.image_id, lease)
    cleanup_media_store(now=clock["now"])
    with pytest.raises(ImageInputError):
        resolve_image_reference(reference, owner_id="conversation-a")


def test_two_processes_resolve_authorized_shared_artifact(isolated_media_store):
    artifact = create_image_artifact(
        _png("purple"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    reference = register_image_artifact(artifact, owner_id="shared-conversation")
    script = (
        "import json,sys; import osia.image_inputs as m; "
        "m.CONFIG_DIR=sys.argv[1]; "
        "r=json.loads(sys.argv[2]); "
        "a=m.resolve_image_reference(r,owner_id=sys.argv[3]); "
        "print(a.content_hash)"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(isolated_media_store),
            json.dumps(reference),
            "shared-conversation",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert completed.stdout.strip() == artifact.content_hash


def test_other_process_cannot_resolve_unauthorized_artifact(isolated_media_store):
    artifact = create_image_artifact(
        _png("orange"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="owner-a")
    script = (
        "import json,sys; import osia.image_inputs as m; "
        "m.CONFIG_DIR=sys.argv[1]; r=json.loads(sys.argv[2]); "
        "\ntry: m.resolve_image_reference(r,owner_id='owner-b')"
        "\nexcept m.ImageInputError as e: print(e.reason_code)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(isolated_media_store), json.dumps(reference)],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert completed.stdout.strip() == "IMAGE_OWNER_MISMATCH"


def _delivery(reference, envelope):
    return prepare_media_delivery(
        reference,
        owner_id="conversation-a",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id=("call-a" if envelope is MediaEnvelopeType.INPUT_IMAGE else ""),
        rpc_id=("rpc-a" if envelope is MediaEnvelopeType.INPUT_IMAGE else ""),
        envelope_type=envelope,
    )


def test_delivery_commit_is_canonical_dedup_boundary(isolated_media_store):
    artifact = create_image_artifact(
        _png("cyan"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    first = _delivery(reference, MediaEnvelopeType.LOCAL_IMAGE)
    assert first.state == MediaDeliveryState.PREPARED.value
    assert commit_media_delivery(first.delivery_id).state == MediaDeliveryState.COMMITTED.value
    duplicate = _delivery(reference, MediaEnvelopeType.LOCAL_IMAGE)
    assert duplicate.delivery_id == first.delivery_id
    assert duplicate.state == MediaDeliveryState.COMMITTED.value


def test_failed_commit_can_retry_but_committed_delivery_cannot_duplicate(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("yellow"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    prepared = _delivery(reference, MediaEnvelopeType.LOCAL_IMAGE)
    assert fail_media_delivery(prepared.delivery_id).state == MediaDeliveryState.FAILED.value
    retry = _delivery(reference, MediaEnvelopeType.LOCAL_IMAGE)
    assert retry.state == MediaDeliveryState.PREPARED.value
    assert commit_media_delivery(retry.delivery_id).state == MediaDeliveryState.COMMITTED.value
    assert media_delivery_record(retry.delivery_id).state == MediaDeliveryState.COMMITTED.value


def test_local_and_dynamic_envelopes_share_artifact_not_delivery_identity(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("white"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    local = _delivery(reference, MediaEnvelopeType.LOCAL_IMAGE)
    dynamic = _delivery(reference, MediaEnvelopeType.INPUT_IMAGE)
    assert local.image_id == dynamic.image_id == artifact.image_id
    assert local.delivery_id != dynamic.delivery_id
    assert local.envelope_type == "localImage"
    assert dynamic.envelope_type == "inputImage"


def test_dynamic_registration_precedes_flush_and_sync_successor_is_repairable():
    source = inspect.getsource(AppServerClient._resolve_dynamic_tool_call)
    assert source.index("attempt.visual_frame_calls[frame_key]") < source.index(
        "self._commit_dynamic_response("
    )
    client = AppServerClient(base_dir=Path(os.getcwd()) / ".unused-test-runtime")
    attempt = _TurnAttempt(thread_id="thread")
    attempt.visual_frame_calls["frame"] = {
        "call_id": "visual-call",
        "arguments": {"target": "fixture"},
            "delivery_id": "delivery-sync-race",
            "delivery_state": "PREPARED",
            "transport_started": True,
            "response_committed": True,
    }
    client._mark_visual_input_consumed(
        attempt,
        successor_item_id="assistant-item",
    )
    assert "visual-call" in attempt.visual_consumed_calls
    assert attempt.visual_frame_calls["frame"]["successor_observed"] is True


def test_unrelated_historical_image_is_not_replayed_but_explicit_followup_is():
    old = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe Saturn"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD"}},
        ],
    }
    unrelated = _native_rebuild_history([
        old,
        {"role": "assistant", "content": "Saturn"},
        {"role": "user", "content": PHYSICAL_PHRASE},
    ])
    assert "OLD" not in json.dumps(unrelated)
    followup = _native_rebuild_history([
        old,
        {"role": "assistant", "content": "Saturn"},
        {"role": "user", "content": "what about this image?"},
    ])
    assert "OLD" in json.dumps(followup)


def test_non_reusable_prepare_applies_historical_image_relevance(monkeypatch):
    class FakeClient:
        generation = 1

        def __init__(self):
            self.started = []

        def start_or_resume_conversation(self, **kwargs):
            self.started.append(kwargs)
            return f"thread-{len(self.started)}"

    monkeypatch.setattr(
        "osia.chatgpt_account.provider._canonical_native_tools",
        lambda *_args, **_kwargs: (),
    )
    provider = ChatGPTAccountProvider(FakeClient())
    image_url = (
        "data:image/png;base64,"
        + base64.b64encode(_png("purple")).decode("ascii")
    )
    historical = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe Saturn"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
    }

    def rebuild(latest_text: str, conversation_id: str) -> list[dict]:
        _state, delta = provider._prepare(
            conversation_id,
            "model",
            [
                {"role": "system", "content": "system"},
                historical,
                {"role": "assistant", "content": "Saturn"},
                {"role": "user", "content": latest_text},
            ],
        )
        return delta

    unrelated = rebuild(PHYSICAL_PHRASE, "conversation-unrelated")
    assert image_url not in json.dumps(unrelated)

    english = rebuild(
        "Give me another visible detail on this same image.",
        "conversation-english-followup",
    )
    french = rebuild(
        "Donne-moi un autre détail visible sur cette même image.",
        "conversation-french-followup",
    )
    assert image_url in json.dumps(english)
    assert image_url in json.dumps(french)


def test_tool_schema_change_invalidates_same_name_native_thread(monkeypatch):
    class FakeClient:
        generation = 1

        def __init__(self):
            self.started = []

        def start_or_resume_conversation(self, **kwargs):
            self.started.append(kwargs)
            return f"thread-{len(self.started)}"

    fake = FakeClient()
    provider = ChatGPTAccountProvider(fake)
    tools = [{
        "type": "function",
        "name": "inspect_desktop_surfaces",
        "description": "read",
        "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}},
    }]
    monkeypatch.setattr(
        "osia.chatgpt_account.provider._canonical_native_tools",
        lambda *_args, **_kwargs: tools,
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
    ]
    state, _delta = provider._prepare("conversation", "model", messages)
    state.expected_messages = [
        _public_message(message) for message in messages if message["role"] != "system"
    ]
    tools[0] = {
        **tools[0],
        "inputSchema": {
            "type": "object",
            "properties": {"target": {"type": "string"}, "refresh": {"type": "boolean"}},
        },
    }
    provider._prepare(
        "conversation",
        "model",
        [*messages, {"role": "user", "content": "second"}],
    )
    assert len(fake.started) == 2


def test_route_bound_capability_uses_exact_metadata_not_model_name():
    sol = effective_route_capabilities(
        "chatgpt_account",
        "gpt-5.6-sol",
        model_metadata={
            "id": "gpt-5.6-sol",
            "inputModalities": ["text", "image"],
        },
    )
    terra = effective_route_capabilities(
        "chatgpt_account",
        "gpt-5.6-terra",
        model_metadata={
            "id": "gpt-5.6-terra",
            "inputModalities": ["text", "image"],
        },
    )
    unknown = effective_route_capabilities("unknown", "vision-in-name")
    assert sol.vision_supported is True and terra.vision_supported is True
    assert unknown.vision_supported is None and unknown.metadata_known is False


def test_log_serializer_redacts_data_urls_to_metadata_only():
    raw = '{"content":{"imageUrl":"data:image/png;base64,aGVsbG8="},"id":7}'
    redacted = redact_app_server_log_body(raw)
    assert "data:image" not in redacted
    assert "aGVsbG8=" not in redacted
    assert "mime=image/png" in redacted and "bytes=5" in redacted
    assert '"id":7' in redacted


@pytest.mark.parametrize(
    "raw",
    (
        '{"token":"ABC123","colors":["red"]}',
        '```json\n{"token":"ABC123","colors":["red"]}\n```',
        'Result: {"token":"ABC123","colors":["red"]}',
    ),
)
def test_controlled_probe_accepts_one_wrapped_json_object(raw):
    assert _parse_probe_json_object(raw) == {
        "token": "ABC123",
        "colors": ["red"],
    }


def test_controlled_probe_selects_final_post_tool_json_object():
    assert _parse_probe_json_object(
        '{"token":"EARLY","colors":[]}\n'
        '{"token":"FINAL","colors":["red"]}'
    ) == {"token": "FINAL", "colors": ["red"]}


def test_bounded_log_migration_preserves_row_and_nonimage_metadata(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client.home_dir.mkdir(parents=True)
    database = client.home_dir / "logs_2.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, "
        "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "INSERT INTO logs VALUES (1, ?, 1)",
        ('{"event":"dynamic","image":"data:image/png;base64,aGVsbG8="}',),
    )
    connection.commit()
    connection.close()
    assert client.redact_operational_image_logs(limit=8, compact=True) == 1
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT id, feedback_log_body FROM logs WHERE id = 1"
    ).fetchone()
    connection.close()
    assert row[0] == 1 and '"event":"dynamic"' in row[1]
    assert "data:image" not in row[1] and "aGVsbG8=" not in row[1]
    raw_url = b"data:image/png;base64,aGVsbG8="
    for suffix in ("", "-wal", "-shm"):
        storage = Path(str(database) + suffix)
        if storage.exists():
            assert raw_url not in storage.read_bytes()


def test_log_compaction_purges_deleted_pixel_remnants_without_logical_rows(
    tmp_path,
):
    client = AppServerClient(base_dir=tmp_path)
    client.home_dir.mkdir(parents=True)
    database = client.home_dir / "logs_2.sqlite"
    raw_url = "data:image/png;base64," + ("A" * 12_000)
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, "
        "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    connection.executemany(
        "INSERT INTO logs VALUES (?, ?, ?)",
        [(index, raw_url, len(raw_url)) for index in range(1, 65)],
    )
    connection.commit()
    connection.execute("DELETE FROM logs")
    connection.commit()
    free_before = connection.execute("PRAGMA freelist_count").fetchone()[0]
    connection.close()
    assert free_before > 0
    assert raw_url[:128].encode("ascii") in database.read_bytes()

    assert client.redact_operational_image_logs(limit=8, compact=True) == 0

    connection = sqlite3.connect(database)
    free_after = connection.execute("PRAGMA freelist_count").fetchone()[0]
    connection.close()
    assert free_after == 0
    assert raw_url[:128].encode("ascii") not in database.read_bytes()


def test_async_log_redactor_catches_a_delayed_native_insert(
    tmp_path,
    monkeypatch,
):
    client = AppServerClient(base_dir=tmp_path)
    client.home_dir.mkdir(parents=True)
    database = client.home_dir / "logs_2.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, "
        "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        app_server_client_module,
        "_IMAGE_LOG_REDACTION_POLL_SECONDS",
        0.01,
    )
    raw_url = "data:image/png;base64,aGVsbG8="
    client._image_log_redaction_pending = True
    client._schedule_operational_log_redaction()
    try:
        time.sleep(0.03)
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT INTO logs VALUES (1, ?, ?)",
            (raw_url, len(raw_url)),
        )
        connection.commit()
        connection.close()
        deadline = time.monotonic() + 2.0
        while (
            time.monotonic() < deadline
            and client.operational_image_log_residual_count() != 0
        ):
            time.sleep(0.01)
        assert client.operational_image_log_residual_count() == 0
    finally:
        client.shutdown()
    assert client._image_log_redaction_pending is False
    assert raw_url.encode("ascii") not in database.read_bytes()


def test_shutdown_drains_more_than_one_cleanup_batch(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client.home_dir.mkdir(parents=True)
    database = client.home_dir / "logs_2.sqlite"
    raw_url = '{"image":"data:image/png;base64,aGVsbG8="}'
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, "
        "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    connection.executemany(
        "INSERT INTO logs VALUES (?, ?, ?)",
        [(index, raw_url, len(raw_url)) for index in range(1, 301)],
    )
    connection.commit()
    connection.close()

    client.shutdown()

    assert client.operational_image_log_residual_count() == 0
    assert client._image_log_redaction_pending is False
    assert b"data:image/png;base64,aGVsbG8=" not in database.read_bytes()


def test_startup_sized_log_cleanup_leaves_no_raw_image_rows(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client.home_dir.mkdir(parents=True)
    database = client.home_dir / "logs_2.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, "
        "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    connection.executemany(
        "INSERT INTO logs VALUES (?, ?, 1)",
        [
            (
                index,
                '{"event":"dynamic","image":"data:image/png;base64,aGVsbG8="}',
            )
            for index in range(1, 301)
        ],
    )
    connection.commit()
    connection.close()
    assert client.redact_operational_image_logs(
        limit=16_384, compact=True,
    ) == 300
    assert client.operational_image_log_residual_count() == 0


def test_log_cleanup_ignores_harmless_data_image_text(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    client.home_dir.mkdir(parents=True)
    database = client.home_dir / "logs_2.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE logs (id INTEGER PRIMARY KEY, feedback_log_body TEXT, "
        "estimated_bytes INTEGER NOT NULL DEFAULT 0)"
    )
    harmless = "diagnostic: data:image was mentioned without a data URL"
    connection.execute("INSERT INTO logs VALUES (1, ?, ?)", (
        harmless, len(harmless.encode("utf-8")),
    ))
    connection.commit()
    connection.close()

    assert client.redact_operational_image_logs(limit=8) == 0
    assert client.operational_image_log_residual_count() == 0
    connection = sqlite3.connect(database)
    persisted = connection.execute(
        "SELECT feedback_log_body FROM logs WHERE id = 1"
    ).fetchone()[0]
    connection.close()
    assert persisted == harmless


def test_temp_cleanup_removes_only_expired_osia_owned_names(tmp_path):
    vision = tmp_path / "vision-owned.png"
    fixed = tmp_path / "osia_kbd_before.png"
    unrelated = tmp_path / "private-photo.png"
    for path in (vision, fixed, unrelated):
        path.write_bytes(b"x")
        os.utime(path, (1, 1))
    removed = cleanup_owned_temp_images(
        directories=(tmp_path,),
        now=10_000,
        minimum_age_seconds=10,
    )
    assert removed == 2
    assert not vision.exists() and not fixed.exists()
    assert unrelated.exists()


def test_normal_media_records_contain_no_pixels_or_data_urls(isolated_media_store):
    artifact = create_image_artifact(
        _png("black"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    delivery = _delivery(reference, MediaEnvelopeType.LOCAL_IMAGE)
    serialized = json.dumps({
        "reference": reference,
        "artifact": artifact.safe_metadata(),
        "delivery": delivery.safe_dict(),
    })
    assert "data:image" not in serialized
    assert "base64" not in serialized.casefold()
    assert _png("black").hex() not in serialized


def test_unknown_prenominal_multiword_application_keeps_the_noun_phrase():
    strategy = build_execution_strategy(
        "inspect Acme Suite window and tell me what you see",
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    assert strategy.goal.required_application == "acme suite"
    assert strategy.goal.target_hint["exactness"] is False
    assert strategy.goal.target_hint["candidates"][0] == "acme suite"


def test_exact_model_target_wins_when_a_weak_hint_matches_another_scene(monkeypatch):
    strategy = build_execution_strategy(
        "inspect alpha suite window and tell me what you see",
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    beta = _scene("beta", "Beta Suite", "beta-window")
    monkeypatch.setattr(desktop_scene, "observe_foreground_scene", lambda **_kw: beta)
    with execution_strategy_scope("x", strategy=strategy):
        selection = desktop_scene._select_desktop_target("Beta Suite")
    assert selection.status == "exact_target"
    assert selection.target_scene is beta
    assert selection.requested_application == "beta suite"
    assert [item["value"] for item in selection.resolved_candidates] == [
        "beta suite"
    ]


def test_explicit_title_target_keeps_authority_over_stronger_process_rank(monkeypatch):
    strategy = build_execution_strategy(
        "inspect the window dcv2 and tell me what you see",
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    msi = _scene("dcv2", "MSI Center", "msi-rank-window")
    monkeypatch.setattr(desktop_scene, "observe_foreground_scene", lambda **_kw: msi)
    with execution_strategy_scope("x", strategy=strategy):
        selection = desktop_scene._select_desktop_target("MSI Center")
    assert selection.requested_application == "msi center"
    assert selection.identity_evidence.startswith("exact_window_title")


def test_semantic_title_alias_survives_exact_surface_refresh_checks():
    msi = _scene("dcv2", "MSI Center", "msi-window")
    assert desktop_scene._scene_matches_application(msi, "MSI Center")
    assert desktop_scene._surface_matches_application(
        msi.window_ref, "MSI Center",
    )
    assert not desktop_scene._scene_matches_application(msi, "Steam")


def test_visual_success_is_bound_to_the_pending_application():
    strategy = build_execution_strategy(
        PHYSICAL_PHRASE,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    base = {
        "frame_id": "frame-1",
        "image_input_sent": True,
        "image_input_consumed": True,
        "exact_surface_pixels": True,
        "window_generation": "generation-1",
        "provider_id": "chatgpt_account",
        "model_id": "gpt-5.6-sol",
        "target_conflict": False,
    }
    strategy._latest_desktop_observation = {
        "visual_evidence_history": [{
            **base,
            "application": "steamwebhelper",
            "requested_application": "steam",
            "resolved_target_candidates": [{"value": "steam"}],
        }],
    }
    assert strategy.mark_visual_answer_ready(
        evidence="normal_output",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
    ) == ()
    strategy._latest_desktop_observation["visual_evidence_history"] = [{
        **base,
        "application": "dcv2",
        "requested_application": "dcv2",
        "resolved_target_candidates": [
            {"value": "dcv2"}, {"value": "msi center"},
        ],
    }]
    assert strategy.mark_visual_answer_ready(
        evidence="normal_output",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
    )


def test_turn_scoped_desktop_media_rejects_another_turn(isolated_media_store):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    owner = turn_scoped_media_owner("conversation-a", "turn-a")
    reference = register_image_artifact(artifact, owner_id=owner)
    assert reference["owner_scope"] == "turn"
    assert resolve_image_reference(reference, owner_id=owner).image_id == artifact.image_id
    with pytest.raises(ImageInputError) as exc:
        resolve_image_reference(
            reference,
            owner_id=turn_scoped_media_owner("conversation-a", "turn-b"),
        )
    assert exc.value.reason_code == "IMAGE_OWNER_MISMATCH"


def test_generic_provider_resolves_turn_scoped_desktop_ref_with_exact_turn(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    reference = register_image_artifact(
        artifact,
        owner_id=turn_scoped_media_owner("conversation-a", "turn-a"),
    )
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "inspect"}, reference,
    ]}]
    wire = image_inputs.adapt_messages_for_provider(
        messages,
        "openai",
        owner_id="conversation-a",
        turn_id="turn-a",
    )
    assert wire[0]["content"][1]["type"] == "image_url"
    with pytest.raises(ImageInputError) as exc:
        image_inputs.adapt_messages_for_provider(
            messages,
            "openai",
            owner_id="conversation-a",
            turn_id="turn-b",
        )
    assert exc.value.reason_code == "IMAGE_OWNER_MISMATCH"


def test_chatgpt_wire_preserves_turn_scoped_media_owner(isolated_media_store):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    owner = turn_scoped_media_owner("conversation-a", "turn-a")
    reference = register_image_artifact(artifact, owner_id=owner)

    wire = image_inputs.adapt_messages_for_provider(
        [{"role": "user", "content": [reference]}],
        "chatgpt_account",
        owner_id="conversation-a",
        turn_id="turn-a",
    )

    media_reference = wire[0]["content"][0]["_osia_media"]
    assert media_reference["owner_scope"] == "turn"
    assert resolve_image_reference(
        media_reference, owner_id=owner,
    ).image_id == artifact.image_id
    rewired = image_inputs.adapt_messages_for_provider(
        wire,
        "chatgpt_account",
        owner_id="conversation-a",
        turn_id="turn-a",
    )
    assert rewired[0]["content"][0]["_osia_media"] == media_reference


def test_chatgpt_raw_image_is_registered_for_staging(isolated_media_store):
    raw = (
        "data:image/png;base64,"
        + base64.b64encode(_png("teal")).decode("ascii")
    )
    wire = image_inputs.adapt_messages_for_provider(
        [{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": raw},
        }]}],
        "chatgpt_account",
        owner_id="conversation-a",
    )

    media_reference = wire[0]["content"][0]["_osia_media"]
    assert media_reference["owner_scope"] == "conversation"
    assert resolve_image_reference(
        media_reference, owner_id="conversation-a",
    ).content_hash == media_reference["content_hash"]
    rewired = image_inputs.adapt_messages_for_provider(
        wire, "chatgpt_account", owner_id="conversation-a",
    )
    assert rewired[0]["content"][0]["_osia_media"] == media_reference


def test_last_owner_release_under_lease_cannot_rebind_another_conversation(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("purple"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(
        artifact, owner_id="conversation-a",
    )
    lease = acquire_image_lease(
        reference,
        owner_id="conversation-a",
        lease_owner_id="delivery-a",
    )

    assert image_inputs.release_image_owner(
        artifact.image_id, owner_id="conversation-a",
    )
    with image_inputs._IMAGE_STORE_LOCK:
        image_inputs._IMAGE_STORE.pop(artifact.image_id, None)
        image_inputs._IMAGE_STORE_BYTES = 0
    with pytest.raises(ImageInputError) as exc:
        resolve_image_reference(reference, owner_id="conversation-b")
    assert exc.value.reason_code == "IMAGE_OWNER_MISMATCH"

    assert release_image_lease(artifact.image_id, lease)
    with pytest.raises(ImageInputError) as exc:
        resolve_image_reference(reference, owner_id="conversation-a")
    assert exc.value.reason_code == "IMAGE_REF_UNAVAILABLE"


def test_media_delivery_claim_is_single_flight_and_transitions_are_monotonic(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("cyan"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    kwargs = dict(
        owner_id="conversation-a",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        envelope_type=MediaEnvelopeType.LOCAL_IMAGE,
        transport_id="native-thread-a",
    )
    first = prepare_media_delivery(reference, **kwargs)
    second = prepare_media_delivery(reference, **kwargs)
    assert first.send_authorized is True
    assert second.state == "PREPARED" and second.send_authorized is False
    committed = commit_media_delivery(first.delivery_id)
    assert committed.state == "COMMITTED"
    assert fail_media_delivery(first.delivery_id).state == "COMMITTED"
    assert image_inputs.acknowledge_media_delivery(first.delivery_id).state == "ACKNOWLEDGED"
    assert fail_media_delivery(first.delivery_id).state == "ACKNOWLEDGED"


def test_dispatching_delivery_fails_closed_across_transport_crash(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("cyan"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    kwargs = dict(
        owner_id="conversation-a",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        envelope_type=MediaEnvelopeType.LOCAL_IMAGE,
        transport_id="native-thread-a",
    )
    prepared = prepare_media_delivery(reference, **kwargs)
    dispatching = begin_media_delivery(prepared.delivery_id)
    assert dispatching is not None and dispatching.state == "DISPATCHING"

    crash_retry = prepare_media_delivery(reference, **kwargs)
    assert crash_retry.state == "DISPATCHING"
    assert crash_retry.send_authorized is False

    failed = fail_media_delivery(
        dispatching.delivery_id,
        reason_code="IMAGE_DELIVERY_FAILED_BEFORE_COMMIT",
    )
    assert failed is not None and failed.state == "FAILED"
    safe_retry = prepare_media_delivery(reference, **kwargs)
    assert safe_retry.state == "PREPARED"
    assert safe_retry.send_authorized is True


def test_bridge_successor_cannot_ack_input_image_before_transport(
    isolated_media_store, tmp_path,
):
    artifact = create_image_artifact(
        _png("violet"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    owner = turn_scoped_media_owner("conversation-a", "turn-a")
    reference = register_image_artifact(artifact, owner_id=owner)
    common = dict(
        owner_id=owner,
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id="call-visual",
    )
    input_delivery = prepare_media_delivery(
        reference,
        **common,
        rpc_id="501",
        transport_id="native-thread-a",
        envelope_type=MediaEnvelopeType.INPUT_IMAGE,
    )
    bridge_delivery = prepare_media_delivery(
        reference,
        **common,
        rpc_id="turn-steer:501",
        transport_id="native-thread-a:codex-0.144-dynamic-image-bridge",
        envelope_type=MediaEnvelopeType.TURN_STEER_IMAGE,
    )
    begin_media_delivery(bridge_delivery.delivery_id)
    commit_media_delivery(bridge_delivery.delivery_id)
    attempt = _TurnAttempt(
        thread_id="native-thread-a",
        conversation_id="conversation-a",
        osia_turn_id="turn-a",
        client_request_id="request-a",
        model_id="gpt-5.6-terra",
    )
    attempt.visual_frame_calls["frame-a"] = {
        "call_id": "call-visual",
        "delivery_id": input_delivery.delivery_id,
        "delivery_state": "PREPARED",
        "transport_started": False,
        "prior_item_ids": [],
    }
    attempt.bridged_visual_delivery_ids.add(
        bridge_delivery.delivery_id
    )
    client = AppServerClient(base_dir=tmp_path / "app-server")

    assert client._mark_visual_input_consumed(
        attempt, successor_item_id="msg-before-dynamic-response",
    ) is False
    assert media_delivery_record(input_delivery.delivery_id).state == "PREPARED"
    assert media_delivery_record(bridge_delivery.delivery_id).state == "COMMITTED"

    begin_media_delivery(input_delivery.delivery_id)
    attempt.visual_frame_calls["frame-a"]["transport_started"] = True
    attempt.visual_frame_calls["frame-a"]["response_committed"] = True
    assert client._mark_visual_input_consumed(
        attempt, successor_item_id="msg-after-dynamic-response",
    ) is True
    assert media_delivery_record(input_delivery.delivery_id).state == "ACKNOWLEDGED"
    assert media_delivery_record(bridge_delivery.delivery_id).state == "ACKNOWLEDGED"


def test_synchronous_successor_is_discarded_when_correlated_send_fails(
    isolated_media_store, tmp_path,
):
    artifact = create_image_artifact(
        _png("violet"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    owner = turn_scoped_media_owner("conversation-a", "turn-a")
    reference = register_image_artifact(artifact, owner_id=owner)
    common = dict(
        owner_id=owner,
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id="call-visual",
    )
    input_delivery = prepare_media_delivery(
        reference,
        **common,
        rpc_id="501",
        transport_id="native-thread-a",
        envelope_type=MediaEnvelopeType.INPUT_IMAGE,
    )
    begin_media_delivery(input_delivery.delivery_id)
    bridge_delivery = prepare_media_delivery(
        reference,
        **common,
        rpc_id="turn-steer:501",
        transport_id="native-thread-a:codex-0.144-dynamic-image-bridge",
        envelope_type=MediaEnvelopeType.TURN_STEER_IMAGE,
    )
    begin_media_delivery(bridge_delivery.delivery_id)
    commit_media_delivery(bridge_delivery.delivery_id)
    attempt = _TurnAttempt(
        thread_id="native-thread-a",
        conversation_id="conversation-a",
        osia_turn_id="turn-a",
        client_request_id="request-a",
        model_id="gpt-5.6-terra",
    )
    attempt.visual_frame_calls["frame-a"] = {
        "call_id": "call-visual",
        "delivery_id": input_delivery.delivery_id,
        "delivery_state": "DISPATCHING",
        "transport_started": False,
        "response_committed": False,
        "pending_successor_item_ids": [],
        "prior_item_ids": [],
    }
    attempt.bridged_visual_delivery_ids.add(
        bridge_delivery.delivery_id
    )
    client = AppServerClient(base_dir=tmp_path / "app-server")

    def fail_after_synchronous_successor(_message):
        assert client._mark_visual_input_consumed(
            attempt, successor_item_id="msg-synchronous",
        ) is False
        raise AppServerError("transport failed", category="process_crash")

    client._send = fail_after_synchronous_successor
    with pytest.raises(AppServerError) as exc:
        client._commit_dynamic_response(
            attempt,
            request_id=501,
            call_id="call-visual",
            signature="signature-a",
            result={"contentItems": [], "success": False},
        )
    assert exc.value.category == "TOOL_RESPONSE_MISSING"
    assert attempt.visual_consumed_calls == set()
    assert attempt.visual_frame_calls["frame-a"]["transport_started"] is False
    assert attempt.visual_frame_calls["frame-a"]["response_committed"] is False
    assert media_delivery_record(input_delivery.delivery_id).state == "DISPATCHING"
    assert media_delivery_record(bridge_delivery.delivery_id).state == "COMMITTED"


def test_bridge_state_write_failure_remains_non_resendable(
    isolated_media_store, tmp_path, monkeypatch,
):
    artifact = create_image_artifact(
        _png("violet"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    owner = turn_scoped_media_owner("conversation-a", "turn-a")
    reference = register_image_artifact(artifact, owner_id=owner)
    client = AppServerClient(base_dir=tmp_path / "app-server")
    client._request = lambda *_args, **_kwargs: {
        "turnId": "native-turn-a",
    }
    attempt = _TurnAttempt(
        thread_id="native-thread-a",
        conversation_id="conversation-a",
        osia_turn_id="turn-a",
        client_request_id="request-a",
        model_id="gpt-5.6-terra",
        turn_id="native-turn-a",
    )
    monkeypatch.setattr(
        image_inputs, "commit_media_delivery",
        lambda _delivery_id: (_ for _ in ()).throw(OSError("state write")),
    )
    monkeypatch.setattr(
        image_inputs, "mark_media_delivery_commit_unknown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("fallback state write")
        ),
    )

    assert client._bridge_dynamic_visual_image_for_runtime(
        attempt,
        call_id="call-visual",
        request_id=501,
        image_reference=reference,
        image_artifact=artifact,
        owner_id=owner,
    ) is False
    identity = image_inputs._delivery_identity(
        image_id=artifact.image_id,
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id="call-visual",
        rpc_id="turn-steer:501",
        envelope_type="turnSteerImage",
        transport_id=(
            "generation:0:thread:native-thread-a:"
            "codex-0.144-dynamic-image-bridge"
        ),
    )
    delivery = media_delivery_record("delivery_" + identity[:32])
    assert delivery is not None and delivery.state == "DISPATCHING"
    retry = prepare_media_delivery(
        reference,
        owner_id=owner,
        provider="chatgpt_account",
        model="gpt-5.6-terra",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id="call-visual",
        rpc_id="turn-steer:501",
        transport_id=(
            "generation:0:thread:native-thread-a:"
            "codex-0.144-dynamic-image-bridge"
        ),
        envelope_type=MediaEnvelopeType.TURN_STEER_IMAGE,
    )
    assert retry.send_authorized is False


def test_native_thread_replacement_gets_a_distinct_local_image_delivery(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("blue"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    common = dict(
        owner_id="conversation-a",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        envelope_type=MediaEnvelopeType.LOCAL_IMAGE,
    )
    first = prepare_media_delivery(
        reference, **common, transport_id="native-thread-a",
    )
    commit_media_delivery(first.delivery_id)
    replacement = prepare_media_delivery(
        reference, **common, transport_id="native-thread-b",
    )
    assert replacement.delivery_id != first.delivery_id
    assert replacement.state == "PREPARED"
    assert replacement.send_authorized is True


def test_unknown_delivery_commit_cannot_be_safely_resent(isolated_media_store):
    artifact = create_image_artifact(
        _png("violet"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    kwargs = dict(
        owner_id="conversation-a",
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        envelope_type=MediaEnvelopeType.LOCAL_IMAGE,
        transport_id="native-thread-a",
    )
    delivery = prepare_media_delivery(reference, **kwargs)
    unknown = mark_media_delivery_commit_unknown(delivery.delivery_id)
    assert unknown.state == "COMMIT_UNKNOWN"
    retry = prepare_media_delivery(reference, **kwargs)
    assert retry.state == "COMMIT_UNKNOWN"
    assert retry.send_authorized is False


def test_two_processes_cannot_claim_the_same_delivery(
    isolated_media_store,
):
    artifact = create_image_artifact(
        _png("green"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    script = (
        "import json,sys; import osia.image_inputs as m; "
        "m.CONFIG_DIR=sys.argv[1]; r=json.loads(sys.argv[2]); "
        "d=m.prepare_media_delivery(r,owner_id='conversation-a',"
        "provider='chatgpt_account',model='gpt-5.6-sol',"
        "conversation_id='conversation-a',request_id='request-a',"
        "turn_id='turn-a',envelope_type=m.MediaEnvelopeType.LOCAL_IMAGE,"
        "transport_id='native-thread-a'); "
        "print(json.dumps({'send':d.send_authorized,'state':d.state}))"
    )
    command = [
        sys.executable,
        "-c",
        script,
        str(isolated_media_store),
        json.dumps(reference),
    ]
    first = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    outputs = []
    for process in (first, second):
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        outputs.append(json.loads(stdout))
    assert sorted(item["send"] for item in outputs) == [False, True]
    assert {item["state"] for item in outputs} == {"PREPARED"}


def test_reference_routing_metadata_is_verified(isolated_media_store):
    artifact = create_image_artifact(
        _png("red"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    tampered = {**reference, "source_type": "desktop_capture"}
    with pytest.raises(ImageInputError) as exc:
        resolve_image_reference(tampered, owner_id="conversation-a")
    assert exc.value.reason_code == "IMAGE_REF_METADATA_MISMATCH"


def test_disk_bound_eviction_removes_the_same_in_memory_artifact(
    isolated_media_store, monkeypatch,
):
    monkeypatch.setattr(image_inputs, "MEDIA_STORE_MAX_FILES", 1)
    first = create_image_artifact(
        _png("red"), "image/png", ImageSourceType.USER_UPLOAD,
        created_at=time.time(),
    )
    first_ref = register_image_artifact(first, owner_id="conversation-a")
    second = create_image_artifact(
        _png("blue"), "image/png", ImageSourceType.USER_UPLOAD,
        created_at=time.time() + 0.001,
    )
    register_image_artifact(second, owner_id="conversation-a")
    assert first.image_id not in image_inputs._IMAGE_STORE
    with pytest.raises(ImageInputError):
        resolve_image_reference(first_ref, owner_id="conversation-a")


def test_generic_history_replays_only_an_explicit_latest_image_reference():
    image_a = {"type": "image_ref", "image_id": "img_a", "source_type": "user_upload"}
    image_b = {"type": "image_ref", "image_id": "img_b", "source_type": "user_upload"}
    history = [
        {"role": "user", "content": [{"type": "text", "text": "first"}, image_a]},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": [{"type": "text", "text": "second"}, image_b]},
    ]
    unrelated = _relevant_image_history(history, "inspect the Steam window")
    assert not any(
        isinstance(message.get("content"), list) for message in unrelated
        if isinstance(message, dict)
    )
    followup = _relevant_image_history(
        history, "give me another detail on this same image",
    )
    assert isinstance(followup[-1]["content"], list)
    assert followup[-1]["content"][-1]["image_id"] == "img_b"
    assert not isinstance(followup[0]["content"], list)


def test_expired_historical_image_is_typed_only_for_relevant_followup(
    isolated_media_store,
):
    now = time.time()
    artifact = create_image_artifact(
        _png("navy"),
        "image/png",
        ImageSourceType.USER_UPLOAD,
        created_at=now - 10,
        expires_at=now - 1,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    history = [{"role": "user", "content": [
        {"type": "text", "text": "describe"}, reference,
    ]}]
    unrelated = _relevant_image_history(history, "inspect Steam")
    assert not isinstance(unrelated[0]["content"], list)
    assert not _rehydrate_persisted_image_message(
        unrelated[0], "conversation-a",
    ).get("_image_ref_error")
    relevant = _relevant_image_history(
        history, "another detail on this same image",
    )
    hydrated = _rehydrate_persisted_image_message(
        relevant[0], "conversation-a",
    )
    assert hydrated["_image_ref_error"] == "IMAGE_REF_EXPIRED"


def test_visual_provider_projection_contains_no_duplicate_scene_or_pixels():
    projected = AppServerClient._visual_provider_result({
        "text": "semantic helper text",
        "scene": {"surfaces": [{"label": "private UIA"}]},
        "diagnostics": {"raw": "private"},
        "observation": {"frame_id": "frame-a", "dimensions": {"width": 4, "height": 3}},
        "receipt": {
            "frame_id": "frame-a",
            "capture_method": "win32_windows_graphics_capture",
            "source_pixel_format": "BGRA8_UNORM",
        },
    })
    serialized = json.dumps(projected)
    assert "private UIA" not in serialized
    assert "semantic helper text" not in serialized
    assert "data:image" not in serialized
    assert projected["visual_frame_id"] == "frame-a"
    assert json.loads(projected["text"])["visual_frame_id"] == "frame-a"
    assert projected["receipt"]["source_pixel_format"] == "BGRA8_UNORM"


def test_isolated_app_server_disables_info_body_logging(tmp_path):
    client = AppServerClient(base_dir=tmp_path)
    environment = client.isolated_environment()
    assert environment["RUST_LOG"] == (
        "warn,codex_core::session::handlers=off"
    )


def test_visual_app_server_launch_override_is_scoped_to_visual_client(tmp_path):
    executable = tmp_path / "codex.exe"
    ordinary = AppServerClient(base_dir=tmp_path / "ordinary")
    visual = AppServerClient(
        base_dir=tmp_path / "visual",
        disable_code_mode_host=True,
        runtime_partition="visual-direct",
        compact_logs_on_startup=False,
    )

    safe_args = [
        "--enable", "code_mode",
        "--enable", "code_mode_host",
        "--disable", "code_mode_only",
        "--disable", "shell_tool",
        "--disable", "unified_exec",
        "--disable", "browser_use",
        "--disable", "browser_use_external",
        "--disable", "browser_use_full_cdp_access",
        "--disable", "computer_use",
        "--disable", "apps",
        "--disable", "plugins",
        "--disable", "standalone_web_search",
        "--disable", "in_app_browser",
        "--disable", "in_app_local_automation",
        "--config", 'web_search="disabled"',
        "--config", "tools.web_search=false",
    ]
    assert ordinary._app_server_command(executable) == [
        str(executable), "app-server", "--stdio", "--strict-config",
        *safe_args,
    ]
    assert visual._app_server_command(executable) == [
        str(executable), "app-server", "--stdio", "--strict-config",
        *safe_args,
        "--disable", "code_mode_host",
    ]
    assert visual.workspace_dir.name == "workspace-visual-direct"
    assert visual.temp_dir.name == "temp-visual-direct"
    assert visual._compact_logs_on_startup is False


def test_secondary_visual_runtime_does_not_require_shared_log_compaction(
    tmp_path, monkeypatch,
):
    client = AppServerClient(
        base_dir=tmp_path / "app-server",
        disable_code_mode_host=True,
        runtime_partition="visual-direct",
        compact_logs_on_startup=False,
    )
    monkeypatch.setattr(image_inputs, "cleanup_media_store", lambda: None)
    monkeypatch.setattr(
        image_inputs,
        "cleanup_owned_temp_images",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        client,
        "_drain_operational_image_logs",
        lambda **_kwargs: False,
    )

    client._ensure_layout()

    assert (client.home_dir / "config.toml").is_file()


def test_only_visual_frontier_uses_direct_dynamic_runtime():
    class FakeClient:
        def __init__(self, name):
            self.name = name
            self.generation = 7
            self.started = []

        def start_or_resume_conversation(self, **kwargs):
            self.started.append(dict(kwargs))
            return f"thread-{self.name}"

    ordinary = FakeClient("ordinary")
    visual = FakeClient("visual")
    provider = ChatGPTAccountProvider(
        ordinary,
        direct_visual_client=visual,
    )
    image_tool = {
        "type": "function",
        "name": "request_visual_observation",
        "description": "exact visual read",
        "inputSchema": {"type": "object"},
    }
    read_tool = {
        "type": "function",
        "name": "read_file",
        "description": "bounded read",
        "inputSchema": {"type": "object"},
    }

    with patch(
        "osia.chatgpt_account.provider._canonical_native_tools",
        return_value=[image_tool],
    ):
        visual_state, _delta = provider._prepare(
            "visual-conversation",
            "gpt-5.6-terra",
            [{"role": "user", "content": "inspect MSI Center visually"}],
        )
    with patch(
        "osia.chatgpt_account.provider._canonical_native_tools",
        return_value=[read_tool],
    ):
        ordinary_state, _delta = provider._prepare(
            "ordinary-conversation",
            "gpt-5.6-terra",
            [{"role": "user", "content": "read a file"}],
        )

    assert visual_state.runtime_client is visual
    assert visual_state.thread_id == "thread-visual"
    assert ordinary_state.runtime_client is ordinary
    assert ordinary_state.thread_id == "thread-ordinary"
    assert len(visual.started) == 1
    assert len(ordinary.started) == 1


def test_reset_login_invalidates_visual_runtime_thread_state():
    class FakeClient:
        def __init__(self, name):
            self.name = name
            self.generation = 3
            self.start_count = 0
            self.shutdown_count = 0
            self.reset_count = 0
            self.cancelled = []

        def start_or_resume_conversation(self, **_kwargs):
            self.start_count += 1
            return f"thread-{self.name}-{self.start_count}"

        def shutdown(self):
            self.shutdown_count += 1

        def reset_login(self):
            self.reset_count += 1

        def cancel_turn(self, *, thread_id, **_kwargs):
            self.cancelled.append(thread_id)

    ordinary = FakeClient("ordinary")
    visual = FakeClient("visual")
    provider = ChatGPTAccountProvider(
        ordinary,
        direct_visual_client=visual,
    )
    image_tool = {
        "type": "function",
        "name": "request_visual_observation",
        "description": "exact visual read",
        "inputSchema": {"type": "object"},
    }
    read_tool = {
        "type": "function",
        "name": "read_file",
        "description": "bounded read",
        "inputSchema": {"type": "object"},
    }
    messages = [{"role": "user", "content": "inspect MSI Center visually"}]
    with patch(
        "osia.chatgpt_account.provider._canonical_native_tools",
        return_value=[image_tool],
    ):
        first, _delta = provider._prepare(
            "conversation-a", "gpt-5.6-terra", messages,
        )
    with patch(
        "osia.chatgpt_account.provider._canonical_native_tools",
        return_value=[read_tool],
    ):
        ordinary_state, _delta = provider._prepare(
            "conversation-b",
            "gpt-5.6-terra",
            [{"role": "user", "content": "read a file"}],
        )
    provider.reset_login()
    with patch(
        "osia.chatgpt_account.provider._canonical_native_tools",
        return_value=[image_tool],
    ):
        replacement, _delta = provider._prepare(
            "conversation-a", "gpt-5.6-terra", messages,
        )

    assert first.thread_id == "thread-visual-1"
    assert ordinary_state.thread_id == "thread-ordinary-1"
    assert replacement.thread_id == "thread-visual-2"
    assert visual.shutdown_count == 1
    assert visual.cancelled == ["thread-visual-1"]
    assert ordinary.reset_count == 1
    assert ordinary.cancelled == ["thread-ordinary-1"]


def test_same_conversation_turns_are_serialized_across_runtime_clients(
    monkeypatch,
):
    visual_started = threading.Event()
    release_visual = threading.Event()
    ordinary_attempted = threading.Event()

    class FakeClient:
        def __init__(self, name):
            self.name = name
            self.generation = 11
            self.started = []
            self.cancelled = []

        def start_or_resume_conversation(self, **_kwargs):
            thread_id = f"thread-{self.name}-{len(self.started) + 1}"
            self.started.append(thread_id)
            return thread_id

        def run_turn(self, **kwargs):
            if self.name == "visual":
                visual_started.set()
                assert release_visual.wait(5.0)
            return app_server_client_module.TurnResult(
                text=f"{self.name}-done",
                thread_id=kwargs["thread_id"],
                turn_id=f"turn-{self.name}",
                status="completed",
            )

        def cancel_turn(self, *, thread_id, **_kwargs):
            self.cancelled.append(thread_id)

    ordinary = FakeClient("ordinary")
    visual = FakeClient("visual")
    provider = ChatGPTAccountProvider(
        ordinary,
        direct_visual_client=visual,
    )
    image_tool = {
        "type": "function",
        "name": "request_visual_observation",
        "description": "exact visual read",
        "inputSchema": {"type": "object"},
    }
    read_tool = {
        "type": "function",
        "name": "read_file",
        "description": "bounded read",
        "inputSchema": {"type": "object"},
    }
    errors = []

    def tools_for_strategy(strategy):
        objective = str(strategy.mission.objective or "")
        return [image_tool] if objective == "visual" else [read_tool]

    def run_visual():
        try:
            with execution_strategy_scope("visual", ("request_visual_observation",)):
                provider.complete(
                    conversation_id="conversation-a",
                    model="gpt-5.6-terra",
                    messages=[{"role": "user", "content": "visual"}],
                )
        except Exception as exc:
            errors.append(exc)

    def run_ordinary():
        try:
            with execution_strategy_scope("ordinary", ("read_file",)):
                ordinary_attempted.set()
                provider.complete(
                    conversation_id="conversation-a",
                    model="gpt-5.6-terra",
                    messages=[
                        {"role": "user", "content": "visual"},
                        {"role": "assistant", "content": "visual-done"},
                        {"role": "user", "content": "ordinary"},
                    ],
                )
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(
        "osia.chatgpt_account.provider._canonical_native_tools",
        tools_for_strategy,
    )
    visual_thread = threading.Thread(target=run_visual)
    ordinary_thread = threading.Thread(target=run_ordinary)
    visual_thread.start()
    assert visual_started.wait(5.0)
    ordinary_thread.start()
    assert ordinary_attempted.wait(5.0)
    assert not ordinary.started
    provider.cancel_conversation("conversation-a")
    assert visual.cancelled == ["thread-visual-1"]
    release_visual.set()
    visual_thread.join(5.0)
    ordinary_thread.join(5.0)

    assert not visual_thread.is_alive()
    assert not ordinary_thread.is_alive()
    assert not errors
    assert ordinary.started == ["thread-ordinary-1"]


def test_reset_login_preserves_active_conversation_lock_identity(monkeypatch):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_count = 0

    class FakeClient:
        generation = 1

        def cancel_turn(self, **_kwargs):
            return None

        def shutdown(self):
            return None

        def reset_login(self):
            return None

    ordinary = FakeClient()
    visual = FakeClient()
    provider = ChatGPTAccountProvider(
        ordinary,
        direct_visual_client=visual,
    )

    def serialized(**_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_entered.set()
            assert release_first.wait(5.0)
        else:
            second_entered.set()
        return app_server_client_module.TurnResult(
            text="done",
            thread_id="thread",
            turn_id="turn",
            status="completed",
        )

    monkeypatch.setattr(provider, "_complete_serialized", serialized)

    def run_complete():
        provider.complete(
            conversation_id="conversation-a",
            model="gpt-5.6-terra",
            messages=[{"role": "user", "content": "test"}],
        )

    first = threading.Thread(target=run_complete)
    second = threading.Thread(target=run_complete)
    first.start()
    assert first_entered.wait(5.0)
    provider.reset_login()
    second.start()
    assert not second_entered.wait(0.1)
    release_first.set()
    first.join(5.0)
    second.join(5.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_reset_login_blocks_new_thread_preparation_until_reset_finishes(
    monkeypatch,
):
    reset_entered = threading.Event()
    release_reset = threading.Event()
    complete_attempted = threading.Event()

    class FakeClient:
        generation = 5

        def __init__(self):
            self.start_count = 0

        def reset_login(self):
            reset_entered.set()
            assert release_reset.wait(5.0)

        def start_or_resume_conversation(self, **_kwargs):
            self.start_count += 1
            return f"thread-{self.start_count}"

        def run_turn(self, **kwargs):
            return app_server_client_module.TurnResult(
                text="done",
                thread_id=kwargs["thread_id"],
                turn_id="turn",
                status="completed",
            )

        def cancel_turn(self, **_kwargs):
            return None

    client = FakeClient()
    provider = ChatGPTAccountProvider(client)
    read_tool = {
        "type": "function",
        "name": "read_file",
        "description": "bounded read",
        "inputSchema": {"type": "object"},
    }
    errors = []

    def reset():
        try:
            provider.reset_login()
        except Exception as exc:
            errors.append(exc)

    def complete():
        try:
            complete_attempted.set()
            provider.complete(
                conversation_id="new-conversation",
                model="gpt-5.6-terra",
                messages=[{"role": "user", "content": "read"}],
            )
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(
        "osia.chatgpt_account.provider._canonical_native_tools",
        lambda _strategy: [read_tool],
    )
    reset_thread = threading.Thread(target=reset)
    complete_thread = threading.Thread(target=complete)
    reset_thread.start()
    assert reset_entered.wait(5.0)
    complete_thread.start()
    assert complete_attempted.wait(5.0)
    assert client.start_count == 0
    release_reset.set()
    reset_thread.join(5.0)
    complete_thread.join(5.0)

    assert not reset_thread.is_alive()
    assert not complete_thread.is_alive()
    assert not errors
    assert client.start_count == 1
    assert provider._threads["new-conversation"].thread_id == "thread-1"


def test_reset_waits_for_app_server_attempt_registration_after_prepare(
    monkeypatch,
):
    turn_input_entered = threading.Event()
    release_turn_input = threading.Event()
    attempt_registered = threading.Event()
    reset_completed = threading.Event()
    release_run_turn = threading.Event()

    class FakeClient:
        generation = 9

        def __init__(self):
            self.cancelled = []
            self.reset_count = 0

        def start_or_resume_conversation(self, **_kwargs):
            return "thread-prepared"

        def run_turn(self, **kwargs):
            kwargs["on_attempt_registered"]()
            attempt_registered.set()
            assert release_run_turn.wait(5.0)
            return app_server_client_module.TurnResult(
                text="done",
                thread_id=kwargs["thread_id"],
                turn_id="turn",
                status="completed",
            )

        def cancel_turn(self, *, thread_id, **_kwargs):
            self.cancelled.append(thread_id)
            release_run_turn.set()

        def reset_login(self):
            self.reset_count += 1

    client = FakeClient()
    provider = ChatGPTAccountProvider(client)
    read_tool = {
        "type": "function",
        "name": "read_file",
        "description": "bounded read",
        "inputSchema": {"type": "object"},
    }
    real_turn_input = account_provider_module._turn_input
    errors = []

    def blocked_turn_input(messages):
        turn_input_entered.set()
        assert release_turn_input.wait(5.0)
        return real_turn_input(messages)

    def complete():
        try:
            provider.complete(
                conversation_id="conversation-a",
                model="gpt-5.6-terra",
                messages=[{"role": "user", "content": "read"}],
            )
        except Exception as exc:
            errors.append(exc)

    def reset():
        try:
            provider.reset_login()
            reset_completed.set()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(
        account_provider_module,
        "_canonical_native_tools",
        lambda _strategy: [read_tool],
    )
    monkeypatch.setattr(
        account_provider_module,
        "_turn_input",
        blocked_turn_input,
    )
    complete_thread = threading.Thread(target=complete)
    reset_thread = threading.Thread(target=reset)
    complete_thread.start()
    assert turn_input_entered.wait(5.0)
    reset_thread.start()
    assert not reset_completed.wait(0.1)
    assert not client.cancelled
    release_turn_input.set()
    assert attempt_registered.wait(5.0)
    reset_thread.join(5.0)
    complete_thread.join(5.0)

    assert not reset_thread.is_alive()
    assert not complete_thread.is_alive()
    assert not errors
    assert client.cancelled == ["thread-prepared"]
    assert client.reset_count == 1
    assert "conversation-a" not in provider._threads


def test_dynamic_image_bridge_is_pinned_to_reviewed_runtime_range():
    assert app_server_client_module._DYNAMIC_IMAGE_BRIDGE_RUNTIME_RANGE == (
        app_server_client_module.SUPPORTED_MIN_VERSION,
        app_server_client_module.SUPPORTED_MAX_VERSION,
    )


def test_local_image_stages_exact_canonical_bytes_or_fails(
    isolated_media_store, tmp_path,
):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(artifact, owner_id="conversation-a")
    different = base64.b64encode(_png("red")).decode("ascii")
    part = {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{different}"},
        "_osia_media": reference,
    }
    client = AppServerClient(base_dir=tmp_path / "app-server")
    attempt = _TurnAttempt(
        thread_id="native-thread",
        conversation_id="conversation-a",
        osia_turn_id="turn-a",
        client_request_id="request-a",
        model_id="gpt-5.6-sol",
    )
    with pytest.raises(AppServerError) as exc:
        client._stage_turn_input([part], attempt)
    assert exc.value.category == "IMAGE_REF_METADATA_MISMATCH"


def test_turn_scoped_chatgpt_image_stages_with_exact_turn_owner(
    isolated_media_store, tmp_path,
):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.DESKTOP_CAPTURE,
    )
    reference = register_image_artifact(
        artifact,
        owner_id=turn_scoped_media_owner("conversation-a", "turn-a"),
    )
    wire = image_inputs.adapt_messages_for_provider(
        [{"role": "user", "content": [reference]}],
        "chatgpt_account",
        owner_id="conversation-a",
        turn_id="turn-a",
    )
    client = AppServerClient(base_dir=tmp_path / "app-server")
    attempt = _TurnAttempt(
        thread_id="native-thread",
        conversation_id="conversation-a",
        osia_turn_id="turn-a",
        client_request_id="request-a",
        model_id="gpt-5.6-sol",
    )

    items, paths, delivery_ids = client._stage_turn_input(
        wire[0]["content"], attempt,
    )
    try:
        assert [item["type"] for item in items] == ["localImage"]
        assert Path(items[0]["path"]).read_bytes() == artifact.image_bytes
        assert len(delivery_ids) == 1
        assert media_delivery_record(delivery_ids[0]).state == "PREPARED"
    finally:
        for delivery_id in delivery_ids:
            fail_media_delivery(delivery_id)
        for path in paths:
            path.unlink(missing_ok=True)


def test_turn_start_identity_mismatch_cannot_leave_local_image_prepared(
    isolated_media_store, tmp_path,
):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(
        artifact, owner_id="conversation-a",
    )
    wire = image_inputs.adapt_messages_for_provider(
        [{"role": "user", "content": [reference]}],
        "chatgpt_account",
        owner_id="conversation-a",
        turn_id="turn-a",
    )

    class MismatchedTurnClient(AppServerClient):
        def _request(self, method, params=None, **_kwargs):
            assert method == "turn/start"
            active = self._attempts[params["threadId"]]
            active.turn_id = "native-turn-early"
            return {"turn": {"id": "native-turn-response"}}

    client = MismatchedTurnClient(base_dir=tmp_path / "app-server")
    with pytest.raises(AppServerError) as exc:
        client.run_turn(
            thread_id="native-thread",
            model="gpt-5.6-sol",
            content=wire[0]["content"],
            conversation_id="conversation-a",
            osia_turn_id="turn-a",
            client_request_id="request-a",
        )
    assert exc.value.category == "TOOL_RESPONSE_ID_MISMATCH"
    identity = image_inputs._delivery_identity(
        image_id=artifact.image_id,
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id="",
        rpc_id="",
        envelope_type="localImage",
        transport_id="generation:0:thread:native-thread",
    )
    delivery = media_delivery_record("delivery_" + identity[:32])
    assert delivery is not None and delivery.state == "COMMITTED"


def test_turn_start_state_write_failure_remains_non_resendable(
    isolated_media_store, tmp_path, monkeypatch,
):
    artifact = create_image_artifact(
        _png("navy"), "image/png", ImageSourceType.USER_UPLOAD,
    )
    reference = register_image_artifact(
        artifact, owner_id="conversation-a",
    )
    wire = image_inputs.adapt_messages_for_provider(
        [{"role": "user", "content": [reference]}],
        "chatgpt_account",
        owner_id="conversation-a",
        turn_id="turn-a",
    )

    class MismatchedTurnClient(AppServerClient):
        def _request(self, method, params=None, **_kwargs):
            assert method == "turn/start"
            active = self._attempts[params["threadId"]]
            active.turn_id = "native-turn-early"
            return {"turn": {"id": "native-turn-response"}}

    monkeypatch.setattr(
        image_inputs, "commit_media_delivery",
        lambda _delivery_id: (_ for _ in ()).throw(OSError("state write")),
    )
    monkeypatch.setattr(
        image_inputs, "mark_media_delivery_commit_unknown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("fallback state write")
        ),
    )
    client = MismatchedTurnClient(base_dir=tmp_path / "app-server")
    with pytest.raises(AppServerError):
        client.run_turn(
            thread_id="native-thread",
            model="gpt-5.6-sol",
            content=wire[0]["content"],
            conversation_id="conversation-a",
            osia_turn_id="turn-a",
            client_request_id="request-a",
        )
    identity = image_inputs._delivery_identity(
        image_id=artifact.image_id,
        provider="chatgpt_account",
        model="gpt-5.6-sol",
        conversation_id="conversation-a",
        request_id="request-a",
        turn_id="turn-a",
        tool_call_id="",
        rpc_id="",
        envelope_type="localImage",
        transport_id="generation:0:thread:native-thread",
    )
    delivery = media_delivery_record("delivery_" + identity[:32])
    assert delivery is not None and delivery.state == "DISPATCHING"


def test_capture_quality_rejects_a_single_bright_pixel():
    import numpy as np

    almost_black = np.zeros((120, 160, 3), dtype=np.uint8)
    almost_black[0, 0] = (255, 255, 255)
    assert desktop_scene._pixel_frame_quality(almost_black)[0] == 0.0
    visible = np.zeros((120, 160, 3), dtype=np.uint8)
    visible[20:100, 30:140] = (20, 60, 180)
    quality = desktop_scene._pixel_frame_quality(visible)
    assert quality[0] > 0.1 and quality[1] > 0.0


def test_duplicate_pixels_still_consume_capture_and_refinement_budgets():
    store = VisualObservationStore(
        maximum_captures=2,
        maximum_refinements=2,
    )
    kwargs = dict(
        surface_ref="surface-a",
        capture_kind=CaptureKind.ZOOM_REFINE,
        reason=ObservationReason.SMALL_TARGET,
        width=48,
        height=32,
        capture_epoch=0,
        structural_epoch=1,
        surface_fingerprint="surface-fingerprint",
        png_bytes=_png("navy"),
    )
    first, created = store.add(**kwargs)
    duplicate, duplicate_created = store.add(**kwargs)
    assert created is True and duplicate_created is False
    assert duplicate.observation_id == first.observation_id
    assert store.capture_count == 2
    assert store.refinement_count == 2
    with pytest.raises(RuntimeError, match="visual_observation_budget_exhausted"):
        store.add(**kwargs)


def test_canonical_provider_policy_enforces_image_count_for_every_entry_path():
    artifacts = tuple(
        create_image_artifact(
            _png("blue"), "image/png", ImageSourceType.USER_UPLOAD,
            created_at=1000.0 + index,
        )
        for index in range(6)
    )
    with pytest.raises(ImageInputError) as exc:
        image_inputs._enforce_provider_image_policy(artifacts, "groq")
    assert exc.value.reason_code == "IMAGE_COUNT_LIMIT_EXCEEDED"


def test_exact_capture_preserves_typed_wgc_fallback_provenance(monkeypatch):
    from osia.desktop_surface import SurfaceBounds

    monkeypatch.setattr(
        desktop_scene,
        "_capture_window_graphics_image",
        lambda _hwnd: (_ for _ in ()).throw(
            RuntimeError("windows_graphics_capture_frame_unstable")
        ),
    )
    monkeypatch.setattr(
        "PIL.ImageGrab.grab",
        lambda **_kwargs: Image.new("RGB", (120, 80), (20, 60, 180)),
    )
    monkeypatch.setattr(
        desktop_scene,
        "_window_bounds",
        lambda _hwnd: SurfaceBounds(0, 0, 120, 80),
    )
    image, method, provenance = desktop_scene._capture_exact_window_image(
        101, SurfaceBounds(0, 0, 120, 80),
    )
    assert image.size == (120, 80)
    assert method == "win32_pillow_exact_window"
    assert provenance["fallback_used"] is True
    assert provenance["capture_attempts"][0]["reason_code"] == (
        "windows_graphics_capture_frame_unstable"
    )
    assert provenance["capture_attempts"][1]["outcome"] == "selected"


@pytest.mark.parametrize(
    ("advanced_value", "expected"),
    ((0, False), (2, True)),
)
def test_displayconfig_reads_advanced_color_for_the_matching_source(
    advanced_value, expected,
):
    import ctypes

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def get_sizes(_flags, path_count, mode_count):
        path_count._obj.value = 1
        mode_count._obj.value = 1
        return 0

    def query(_flags, path_count, paths, mode_count, _modes, _topology):
        path_count._obj.value = 1
        mode_count._obj.value = 1
        paths[0].sourceInfo.adapterId.LowPart = 7
        paths[0].sourceInfo.id = 3
        paths[0].targetInfo.adapterId.LowPart = 7
        paths[0].targetInfo.id = 9
        return 0

    def get_info(header_pointer):
        header = header_pointer._obj
        address = ctypes.addressof(header)
        if header.type == 1:
            packet = desktop_scene._DisplayConfigSourceDeviceName.from_address(
                address,
            )
            packet.viewGdiDeviceName = r"\\.\DISPLAY7"
            return 0
        if header.type == 9:
            packet = desktop_scene._DisplayConfigAdvancedColorInfo.from_address(
                address,
            )
            packet.value = advanced_value
            return 0
        return 87

    user32 = SimpleNamespace(
        GetDisplayConfigBufferSizes=Function(get_sizes),
        QueryDisplayConfig=Function(query),
        DisplayConfigGetDeviceInfo=Function(get_info),
    )

    assert desktop_scene._displayconfig_advanced_color_enabled(
        r"\\.\DISPLAY7", user32,
    ) is expected


@pytest.mark.parametrize(
    ("state", "reason"),
    (
        (True, "windows_graphics_capture_advanced_color_unsupported"),
        (None, "windows_graphics_capture_advanced_color_state_unavailable"),
    ),
)
def test_wgc_fails_closed_before_capture_when_color_state_is_unsafe(
    monkeypatch, state, reason,
):
    class CaptureMustNotStart:
        def __init__(self, **_kwargs):
            raise AssertionError("WGC must not start")

    monkeypatch.setattr(
        desktop_scene, "_window_advanced_color_enabled", lambda _hwnd: state,
    )
    monkeypatch.setitem(
        sys.modules,
        "windows_capture",
        SimpleNamespace(WindowsCapture=CaptureMustNotStart),
    )

    with pytest.raises(RuntimeError, match=reason):
        desktop_scene._capture_window_graphics_image(101)


def test_exact_capture_records_advanced_color_wgc_rejection(monkeypatch):
    from osia.desktop_surface import SurfaceBounds

    bounds = SurfaceBounds(0, 0, 120, 80)
    monkeypatch.setattr(
        desktop_scene, "_window_advanced_color_enabled", lambda _hwnd: True,
    )
    monkeypatch.setattr(
        "PIL.ImageGrab.grab",
        lambda **_kwargs: Image.new("RGB", (120, 80), (20, 60, 180)),
    )
    monkeypatch.setattr(desktop_scene, "_window_bounds", lambda _hwnd: bounds)
    monkeypatch.setattr(desktop_scene, "_window_client_bounds", lambda _hwnd: None)

    _image, method, provenance = desktop_scene._capture_exact_window_image(
        101, bounds,
    )

    assert method == "win32_pillow_exact_window"
    assert provenance["capture_attempts"][0] == {
        "capture_driver": "win32_windows_graphics_capture",
        "outcome": "rejected",
        "reason_code": "windows_graphics_capture_advanced_color_unsupported",
    }


def test_static_wgc_single_frame_remains_a_valid_exact_candidate(monkeypatch):
    import numpy as np

    monkeypatch.setattr(
        desktop_scene, "_window_advanced_color_enabled", lambda _hwnd: False,
    )
    pixels = np.empty((80, 120, 4), dtype=np.uint8)
    pixels[:, :, :] = (20, 60, 180, 255)

    class ImmediateEvent:
        def __init__(self):
            self.signaled = False

        def set(self):
            self.signaled = True

        def wait(self, _timeout):
            return self.signaled

    class InternalControl:
        def stop(self):
            return None

    class CaptureControl:
        def stop(self):
            return None

        def is_finished(self):
            return True

        def wait(self):
            return None

    class StaticCapture:
        def __init__(self, **_kwargs):
            self.handlers = {}

        def event(self, handler):
            self.handlers[handler.__name__] = handler
            return handler

        def start_free_threaded(self):
            self.handlers["on_frame_arrived"](
                SimpleNamespace(frame_buffer=pixels), InternalControl(),
            )
            return CaptureControl()

    monkeypatch.setitem(
        sys.modules,
        "windows_capture",
        SimpleNamespace(WindowsCapture=StaticCapture),
    )
    monkeypatch.setattr(desktop_scene.threading, "Event", ImmediateEvent)

    image, provenance = desktop_scene._capture_window_graphics_image(101)

    assert image.size == (120, 80)
    assert provenance["stabilized_frame_count"] == 1
    assert provenance["source_pixel_format"] == "BGRA8_UNORM"

    second_geometry = np.empty((81, 120, 4), dtype=np.uint8)
    second_geometry[:, :, :] = (20, 60, 180, 255)

    class UnstableCapture(StaticCapture):
        def start_free_threaded(self):
            control = InternalControl()
            self.handlers["on_frame_arrived"](
                SimpleNamespace(frame_buffer=pixels), control,
            )
            self.handlers["on_frame_arrived"](
                SimpleNamespace(frame_buffer=second_geometry), control,
            )
            return CaptureControl()

    monkeypatch.setitem(
        sys.modules,
        "windows_capture",
        SimpleNamespace(WindowsCapture=UnstableCapture),
    )
    with pytest.raises(
        RuntimeError, match="windows_graphics_capture_geometry_unstable",
    ):
        desktop_scene._capture_window_graphics_image(101)


def test_wgc_stable_geometry_selects_newest_valid_frame(monkeypatch):
    import numpy as np

    older = np.zeros((80, 120, 4), dtype=np.uint8)
    older[:, :, :] = (0, 0, 255, 255)
    newer = np.zeros((80, 120, 4), dtype=np.uint8)
    newer[8:72, 18:102, :] = (40, 40, 40, 255)

    class ImmediateEvent:
        def __init__(self):
            self.signaled = False

        def set(self):
            self.signaled = True

        def wait(self, _timeout):
            return self.signaled

    class InternalControl:
        def stop(self):
            return None

    class CaptureControl:
        def stop(self):
            return None

        def is_finished(self):
            return True

        def wait(self):
            return None

    class TwoFrameCapture:
        def __init__(self, **_kwargs):
            self.handlers = {}

        def event(self, handler):
            self.handlers[handler.__name__] = handler
            return handler

        def start_free_threaded(self):
            control = InternalControl()
            self.handlers["on_frame_arrived"](
                SimpleNamespace(frame_buffer=older), control,
            )
            self.handlers["on_frame_arrived"](
                SimpleNamespace(frame_buffer=newer), control,
            )
            return CaptureControl()

    monkeypatch.setitem(
        sys.modules,
        "windows_capture",
        SimpleNamespace(WindowsCapture=TwoFrameCapture),
    )
    monkeypatch.setattr(desktop_scene.threading, "Event", ImmediateEvent)
    monkeypatch.setattr(
        desktop_scene, "_window_advanced_color_enabled", lambda _hwnd: False,
    )

    image, provenance = desktop_scene._capture_window_graphics_image(101)

    assert image.getpixel((60, 40)) == (40, 40, 40)
    assert provenance["stabilized_frame_count"] == 2


def test_printwindow_fails_closed_when_physical_mapping_is_unproven(monkeypatch):
    from osia.desktop_surface import SurfaceBounds

    bounds = SurfaceBounds(0, 0, 120, 80)
    monkeypatch.setattr(
        desktop_scene,
        "_capture_window_graphics_image",
        lambda _hwnd: (_ for _ in ()).throw(
            RuntimeError("windows_graphics_capture_frame_unavailable")
        ),
    )
    monkeypatch.setattr(
        "PIL.ImageGrab.grab",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("pillow unavailable")),
    )
    monkeypatch.setattr(desktop_scene, "_window_bounds", lambda _hwnd: bounds)
    monkeypatch.setattr(
        desktop_scene, "_print_window_uses_physical_pixels", lambda _hwnd: False,
    )

    with pytest.raises(desktop_scene._ExactWindowCaptureFailure) as caught:
        desktop_scene._capture_exact_window_image(101, bounds)

    reasons = [item["reason_code"] for item in caught.value.attempts]
    assert reasons[-2:] == [
        "printwindow_physical_mapping_unproven",
        "printwindow_physical_mapping_unproven",
    ]


def test_visual_preflight_rejects_a_surface_from_another_application(monkeypatch):
    strategy = build_execution_strategy(
        PHYSICAL_PHRASE,
        ("inspect_desktop_surfaces", "request_visual_observation"),
    )
    strategy._latest_desktop_observation = {
        "application": "dcv2",
        "requested_application": "msi center",
        "resolved_target_candidates": [{"value": "msi center"}],
        "target_resolution": "exact_target",
        "target_conflict": False,
        "window_surface_ref": "msi-surface",
        "window_generation": "msi-generation",
        "target_minimized": False,
        "observed_at": time.time(),
    }
    surfaces = {
        "msi-surface": SimpleNamespace(
            surface=SimpleNamespace(
                process_ref="msi-process",
                window_generation="msi-generation",
            ),
        ),
        "steam-surface": SimpleNamespace(
            surface=SimpleNamespace(
                process_ref="steam-process",
                window_generation="steam-generation",
            ),
        ),
    }
    monkeypatch.setattr(
        desktop_scene.DESKTOP_SURFACE_REGISTRY,
        "refresh_anchor",
        lambda ref: surfaces[ref],
    )
    assert execution_strategy_module._fresh_exact_visual_target(
        strategy, surface_ref="msi-surface",
    )
    assert not execution_strategy_module._fresh_exact_visual_target(
        strategy, surface_ref="steam-surface",
    )


def test_visual_dynamic_path_has_no_history_refusal_or_retry_budget():
    assert not hasattr(AppServerClient, "_dynamic_material_state_fingerprint")
    assert not hasattr(_TurnAttempt, "dynamic_read_fingerprints")
    assert not hasattr(_TurnAttempt, "dynamic_retryable_failures")


def test_visual_consumption_requires_exact_turn_and_accepts_bound_delta_without_id():
    client = AppServerClient(base_dir=Path(os.getcwd()) / ".unused-test-runtime")
    attempt = _TurnAttempt(
        thread_id="thread-a",
        turn_id="turn-a",
    )
    attempt.visual_frame_calls["frame-a"] = {
        "call_id": "call-vision",
            "delivery_state": "COMMITTED",
            "transport_started": True,
            "response_committed": True,
        "prior_item_ids": [],
        "arguments": {"surface_ref": "surface-a"},
    }
    client._attempts["thread-a"] = attempt
    client._dispatch_notification(
        "item/agentMessage/delta",
        {
            "threadId": "thread-a",
            "turnId": "old-turn",
            "delta": "stale",
        },
    )
    assert not attempt.text_parts
    assert not attempt.visual_consumed_calls
    client._dispatch_notification(
        "item/agentMessage/delta",
        {
            "threadId": "thread-a",
            "turnId": "turn-a",
            "delta": "fresh post-image output",
        },
    )
    assert attempt.text_parts == ["fresh post-image output"]
    assert attempt.visual_consumed_calls == {"call-vision"}
    assert len(attempt.post_visual_agent_items) == 1


def test_pre_response_nested_turn_notification_is_buffered_and_bound_exactly():
    client = AppServerClient(base_dir=Path(os.getcwd()) / ".unused-test-runtime")
    attempt = _TurnAttempt(thread_id="thread-a")
    client._attempts["thread-a"] = attempt
    client._dispatch_notification(
        "turn/completed",
        {
            "threadId": "thread-a",
            "turn": {"id": "turn-a", "status": "completed"},
        },
    )
    assert len(attempt.pending_turn_notifications) == 1
    assert attempt.turn_id is None
    attempt.turn_id = "turn-a"
    method, params = attempt.pending_turn_notifications.pop()
    client._dispatch_notification(method, params)
    kind, payload = attempt.events.get_nowait()
    assert kind == "done"
    assert payload["status"] == "completed"
