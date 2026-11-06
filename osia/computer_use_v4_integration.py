"""Thin Unified Chat adapter for the isolated Computer Use V4 service."""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import secrets
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from osia.computer_use_v4 import (
    ComputerAction,
    ComputerFrame,
    ComputerUseV4,
    ComputerUseV4Error,
    _window_token,
)


COMPUTER_USE_V4_READ_TOOLS = frozenset({
    "computer_screenshot", "computer_zoom", "computer_observation",
})
COMPUTER_USE_V4_MUTATION_TOOLS = frozenset({
    "computer_click", "computer_double_click", "computer_type",
    "computer_keypress", "computer_scroll", "computer_actions",
})
COMPUTER_USE_V4_TOOL_NAMES = (
    "computer_screenshot",
    "computer_zoom",
    "computer_observation",
    "computer_click",
    "computer_double_click",
    "computer_type",
    "computer_keypress",
    "computer_scroll",
    "computer_actions",
)


@dataclass
class _ComputerTurnState:
    provider: str
    model: str
    conversation_id: str
    turn_id: str
    loop_id: str = field(
        default_factory=lambda: "v4chat_" + secrets.token_urlsafe(12),
    )
    loop: ComputerUseV4 | None = None
    observation_count: int = 0
    action_count: int = 0
    callback_count: int = 0
    stale_rejection_count: int = 0
    last_result_status: str = ""
    batch_attempt_id: str = ""
    test_config: dict = field(default_factory=dict, repr=False)


_TURN_STATE: contextvars.ContextVar[_ComputerTurnState | None] = (
    contextvars.ContextVar("osia_computer_use_v4_turn", default=None)
)
_TEST_CONFIG: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "osia_computer_use_v4_test_config", default=None,
)


@contextmanager
def computer_use_v4_test_scope(**config):
    """Controlled dependency injection; production never binds this scope."""

    token = _TEST_CONFIG.set(dict(config))
    try:
        yield
    finally:
        _TEST_CONFIG.reset(token)


@contextmanager
def computer_use_v4_turn_scope(
    provider: str,
    model: str,
    *,
    conversation_id: str,
    turn_id: str,
):
    """Bind one lazy V4 loop to the canonical Unified Chat turn identity."""

    state = _ComputerTurnState(
        provider=str(provider or "").strip().lower(),
        model=str(model or "").strip(),
        conversation_id=str(conversation_id or "").strip(),
        turn_id=str(turn_id or "").strip(),
        test_config=dict(_TEST_CONFIG.get() or {}),
    )
    if not all((state.provider, state.model, state.conversation_id, state.turn_id)):
        raise ValueError("computer_use_v4_turn_identity_incomplete")
    token = _TURN_STATE.set(state)
    try:
        yield state
    finally:
        try:
            if state.loop is not None:
                state.loop.close()
                _trace("capture_closed", {
                    "loop_id": state.loop_id,
                    "provider_id": state.provider,
                    "model_id": state.model,
                    "capture_lifecycle": state.loop.capture_lifecycle_snapshot(),
                })
        finally:
            _TURN_STATE.reset(token)


def current_computer_use_v4_state() -> _ComputerTurnState | None:
    return _TURN_STATE.get()


def _active_config() -> dict:
    state = _TURN_STATE.get()
    if state is not None and state.test_config:
        return state.test_config
    return dict(_TEST_CONFIG.get() or {})


def computer_use_v4_tools_available() -> bool:
    from osia.slash_commands import current_chat_commands

    state = _TURN_STATE.get()
    if state is None or current_chat_commands()[0]:
        return False
    config = _active_config()
    if isinstance(config.get("vision_capable"), bool):
        return bool(config["vision_capable"])
    try:
        from osia.image_inputs import image_input_decision

        return bool(
            image_input_decision(
                state.provider, state.model, ("image/png",),
            ).usable
        )
    except Exception:
        return False


def _current_state() -> _ComputerTurnState:
    from osia.slash_commands import current_chat_commands

    state = _TURN_STATE.get()
    if state is None:
        raise ComputerUseV4Error("TURN_CONTEXT_REQUIRED")
    if current_chat_commands()[0]:
        raise ComputerUseV4Error("CONVERSATION_VISION_DISABLED")
    return state


def _trace(event: str, payload: Mapping[str, Any]) -> None:
    config = _active_config()
    sink = config.get("trace")
    safe = {
        "event": str(event or "")[:80],
        **{
            str(key)[:80]: value
            for key, value in payload.items()
            if key not in {"image_bytes", "image_ref", "text"}
        },
    }
    if isinstance(sink, list):
        sink.append(safe)
    if config.get("trace_print") is True:
        print(
            "[COMPUTER_USE_V4] "
            + json.dumps(safe, sort_keys=True, separators=(",", ":")),
            flush=True,
        )


def _permission_preflight(
    _action: ComputerAction,
    _frame: ComputerFrame,
) -> tuple[bool, str]:
    """Prove the call arrived through OSIA's already-evaluated tool policy."""

    state = _current_state()
    try:
        from osia.tool_attempts import (
            current_tool_attempt_id,
            current_tool_attempt_store,
        )

        attempt_id = str(current_tool_attempt_id() or "")
        store = current_tool_attempt_store()
        record = store.get_attempt(attempt_id) if store is not None and attempt_id else None
    except Exception:
        record = None
    allowed = bool(
        isinstance(record, dict)
        and str(record.get("turn_id") or "") == state.turn_id
        and str(record.get("provider_id") or "") == state.provider
        and str(record.get("model_id") or "") == state.model
        and str(record.get("tool_name") or "") in COMPUTER_USE_V4_MUTATION_TOOLS
        and str(record.get("status") or "") in {"running", "pending"}
    )
    return (
        allowed,
        "unified_chat_permission_preflight_passed"
        if allowed else "unified_chat_permission_preflight_missing",
    )


def _external_action_boundary(
    _action: ComputerAction,
    _frame: ComputerFrame,
    index: int,
):
    """Claim the outer Unified Chat ActionTruth attempt at the OS callback."""

    state = _current_state()
    from osia.tool_attempts import (
        current_tool_attempt_id,
        current_tool_attempt_store,
    )

    attempt_id = str(current_tool_attempt_id() or "")
    store = current_tool_attempt_store()
    if not attempt_id or store is None:
        return None, "unified_chat_actiontruth_missing"
    current = store.get_attempt(attempt_id) or {}
    journal = [
        dict(item) for item in current.get("primitive_journal") or []
        if isinstance(item, Mapping)
    ]
    if journal:
        if not 0 <= index < len(journal):
            return None, "unified_chat_batch_primitive_missing"
        if index == 0:
            current, claimed = store.claim_owner_dispatch(
                attempt_id,
                allow_pending_owner=True,
            )
            if not claimed:
                return None, "unified_chat_dispatch_already_started"
            state.batch_attempt_id = attempt_id
        elif (
            state.batch_attempt_id != attempt_id
            or not current.get("owner_dispatch_claimed_at")
        ):
            return None, "unified_chat_batch_dispatch_not_claimed"
        entry = journal[index]
        try:
            current = store.record_primitive_dispatch_started(
                attempt_id,
                primitive_id=str(entry.get("primitive_id") or ""),
                application_identity=str(
                    entry.get("application_identity") or ""
                ),
                surface_ref=str(entry.get("surface_ref") or ""),
                native_window_id=str(entry.get("native_window_id") or ""),
                window_generation=str(
                    entry.get("window_generation") or ""
                ),
                uia_pattern=str(entry.get("uia_pattern") or ""),
            )
        except Exception:
            return None, "unified_chat_batch_primitive_claim_rejected"
        state.action_count += 1
        return current, "unified_chat_batch_primitive_claimed"
    if index > 0 and state.batch_attempt_id == attempt_id:
        if current.get("owner_dispatch_claimed_at"):
            state.action_count += 1
            return current, "unified_chat_batch_dispatch_already_claimed"
        return None, "unified_chat_batch_dispatch_not_claimed"
    current, claimed = store.claim_owner_dispatch(
        attempt_id,
        allow_pending_owner=True,
    )
    if not claimed:
        return None, "unified_chat_dispatch_already_started"
    state.batch_attempt_id = attempt_id
    state.action_count += 1
    return current, "unified_chat_actiontruth_dispatch_claimed"


def _dispatch_result_verifier(
    result: Any,
    action: ComputerAction,
    frame: ComputerFrame,
) -> bool:
    config = _active_config()
    verifier = config.get("dispatch_result_verifier")
    if not callable(verifier):
        return False
    return bool(verifier(result, action, frame))


def _loop() -> ComputerUseV4:
    state = _current_state()
    if state.loop is not None:
        return state.loop
    config = _active_config()
    state.loop = ComputerUseV4(
        state.provider,
        state.model,
        state.turn_id,
        loop_id=state.loop_id,
        window_state=config.get("window_state"),
        capture_window=config.get("capture_window"),
        foreground_activation=config.get("foreground_activation"),
        action_store=config.get("action_store"),
        dispatchers=config.get("dispatchers"),
        permission_check=_permission_preflight,
        external_action_boundary=_external_action_boundary,
        dispatch_result_verifier=_dispatch_result_verifier,
    )
    return state.loop


def _normalized_window_label(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _resolve_window(window: str = "") -> int:
    config = _active_config()
    resolver = config.get("window_resolver")
    if callable(resolver):
        return int(resolver(str(window or "")) or 0)
    if sys.platform != "win32":
        return 0
    from osia.tools import desktop_control

    requested = _normalized_window_label(window)
    if not requested:
        foreground = desktop_control._foreground_window_win32()
        return int(foreground[0] or 0) if foreground else 0
    candidates = []
    for hwnd, title, _pid, _class_name in desktop_control._enum_app_windows_win32():
        normalized = _normalized_window_label(title)
        if normalized == requested or normalized.startswith(requested + " -"):
            candidates.append(int(hwnd))
    if len(set(candidates)) == 1:
        return candidates[0]
    if candidates:
        raise ComputerUseV4Error("WINDOW_AMBIGUOUS", "select_an_exact_window_surface_ref")
    process_match = desktop_control.find_app_window(window)
    return int(process_match[0]) if process_match is not None else 0


def _capture_target(window: str, surface_ref: str) -> tuple[int, str]:
    if not surface_ref:
        return _resolve_window(window), ""
    if str(window or "").strip():
        raise ComputerUseV4Error("INVALID_CAPTURE_TARGET", "choose_window_or_surface_ref")
    if sys.platform != "win32":
        raise ComputerUseV4Error("CAPTURE_UNSUPPORTED", "exact_window_requires_win32")
    from osia import desktop_scene
    from osia.desktop_surface import SurfaceKind

    scene, surface, reason = desktop_scene._observe_exact_bound_visual_scene(surface_ref)
    if scene is None or surface is None:
        raise ComputerUseV4Error(reason or "WINDOW_NOT_FOUND")
    if surface.surface_kind not in {SurfaceKind.WINDOW, SurfaceKind.DIALOG}:
        raise ComputerUseV4Error("INVALID_CAPTURE_TARGET", "window_surface_ref_required")
    native = desktop_scene.DESKTOP_SURFACE_REGISTRY.native_identity(scene.window_ref.ref_id)
    if native is None:
        return 0, ""
    hwnd = int(native.native_window_id)
    return hwnd, _window_token({
        "hwnd": hwnd, "pid": native.pid,
        "process_started_at": native.process_started_at,
        "window_class": native.class_name,
    })


def _capture_window_frame(window: str, surface_ref: str) -> ComputerFrame:
    hwnd, expected_token = _capture_target(window, surface_ref)
    if hwnd <= 0:
        raise ComputerUseV4Error("WINDOW_NOT_FOUND")
    loop = _loop()
    if expected_token:
        return loop.screenshot(hwnd, expected_window_token=expected_token)
    return loop.screenshot(hwnd)


def _current_visual_postcondition() -> tuple[object, str, str]:
    """Expose the current checkpoint the active model must evaluate."""

    from osia.execution_strategy import current_execution_strategy

    strategy = current_execution_strategy()
    node = (
        strategy.node_by_id(strategy._frozen_node_id)
        if strategy is not None and strategy.frozen_reason else
        strategy.goal if strategy is not None else None
    )
    attempt = (
        strategy.attempt_by_id(strategy._frozen_attempt_id)
        if strategy is not None and strategy.frozen_reason else
        next((
            item for item in reversed(strategy.attempts)
            if node is not None
            and item.node_id == node.node_id
            and item.primitive_postcondition
            and item.status == "selected"
        ), None)
        if strategy is not None else None
    )
    if attempt is not None and attempt.primitive_postcondition:
        return attempt, str(attempt.primitive_postcondition)[:512], "action_effect"
    return (
        attempt,
        str(node.observable_success_postcondition if node is not None else "")[:512],
        "semantic_goal",
    )


def _image_result(frame: ComputerFrame, *, reason_code: str) -> dict[str, Any]:
    state = _current_state()
    from osia.chat_screenshots import remember_chat_observation
    from osia.image_inputs import register_image_artifact, turn_scoped_media_owner

    artifact = frame.to_image_artifact()
    owner_id = turn_scoped_media_owner(
        state.conversation_id, state.turn_id,
    )
    image_ref = register_image_artifact(artifact, owner_id=owner_id)
    remember_chat_observation(state.conversation_id, state.turn_id, image_ref)
    observation_verifier = _active_config().get(
        "observation_verifier"
    )
    authoritative_postcondition = False
    if callable(observation_verifier):
        try:
            authoritative_postcondition = bool(observation_verifier(frame))
        except Exception:
            authoritative_postcondition = False
    state.observation_count += 1
    state.last_result_status = "SCREENSHOT"
    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
        if strategy is not None:
            strategy.observe(
                "fresh Computer Use V4 frame available",
                evidence_token=(
                    "computer_use_v4_frame:"
                    + frame.image_sha256[:24]
                ),
            )
    except Exception:
        pass
    _trace("observation", {
        "loop_id": state.loop_id,
        "frame_id": frame.frame_id,
        "provider_id": state.provider,
        "model_id": state.model,
        "image_width": frame.image_width,
        "image_height": frame.image_height,
        "observation_count": state.observation_count,
        "action_count": state.action_count,
        "capture_method": frame.capture_method,
        "capture_lifecycle": _loop().capture_lifecycle_snapshot(),
    })
    try:
        _checkpoint_attempt, checkpoint, checkpoint_role = (
            _current_visual_postcondition()
        )
    except Exception:
        checkpoint, checkpoint_role = "", ""
    result = {
        "text": json.dumps({
            "frame_id": frame.frame_id,
            "image_dimensions": [frame.image_width, frame.image_height],
            "window": frame.window_title,
            "foreground_at_capture": frame.foreground_at_capture,
            "instruction": (
                "Use x/y in these attached image pixels. Reuse frame_id exactly."
            ),
            "current_visual_postcondition": checkpoint,
            "checkpoint_role": checkpoint_role,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "visual_frame_id": frame.frame_id,
        "image_ref": image_ref,
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "read_only",
            "outcome": "success_verified",
            "reason_code": reason_code,
            "v4_loop_id": state.loop_id,
            "frame_id": frame.frame_id,
            "surface_ref": artifact.surface_id,
            "window_generation": artifact.generation,
            "application": artifact.application_identity,
            "native_window_id": str(frame.hwnd),
            "captured_at": frame.captured_at,
            "expires_at": frame.expires_at,
            "image_id": artifact.image_id,
            "image_sha256": artifact.content_hash,
            "image_mime": artifact.mime_type,
            "image_width": artifact.width,
            "image_height": artifact.height,
            "dimensions": {
                "width": frame.image_width,
                "height": frame.image_height,
            },
            "provider_id": state.provider,
            "model_id": state.model,
            "capture_method": frame.capture_method,
            "capture_attempts": [dict(item) for item in frame.capture_attempts],
            "exact_surface_pixels": True,
            "observation_count": state.observation_count,
            "action_count": state.action_count,
            "authoritative_postcondition_verified": authoritative_postcondition,
        },
    }
    return result


def _error_result(code: str, detail: str = "") -> dict[str, Any]:
    return {
        "text": f"ERROR: Computer Use V4 {code}: {detail or code}.",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "not_dispatched",
            "outcome": "confirmed_failure_before_effect",
            "reason_code": str(code or "COMPUTER_USE_FAILED")[:80],
        },
    }


def tool_computer_screenshot(window: str = "", surface_ref: str = "") -> dict[str, Any]:
    if not computer_use_v4_tools_available():
        return _error_result("MODEL_IMAGE_INPUT_UNAVAILABLE")
    try:
        return _image_result(
            _capture_window_frame(window, surface_ref),
            reason_code="V4_ACTIVE_MODEL_IMAGE_PART_BUILT",
        )
    except ComputerUseV4Error as exc:
        return _error_result(exc.code, exc.detail)


def tool_save_screenshot(window: str = "", surface_ref: str = "") -> dict[str, Any]:
    """Save a fresh window capture as an attachment to this Chat turn."""
    from osia.chat_screenshots import save_chat_screenshot
    from osia.image_inputs import ImageInputError

    try:
        state = _current_state()
        frame = _capture_window_frame(window, surface_ref)
        attachment = save_chat_screenshot(
            state.conversation_id, state.turn_id, frame.to_image_artifact(),
        )
    except ComputerUseV4Error as exc:
        return _error_result(exc.code, exc.detail)
    except (OSError, ValueError, ImageInputError):
        return _error_result("SCREENSHOT_SAVE_FAILED")
    return {
        "text": "Saved a fresh screenshot of the selected window for this chat reply.",
        "receipt": {
            "schema_version": 1, "dispatch_state": "read_only",
            "outcome": "success_verified", "reason_code": "CHAT_SCREENSHOT_SAVED",
            "screenshot_id": attachment["id"], "captured_at": frame.captured_at,
        },
    }


def tool_computer_zoom(
    frame_id: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> dict[str, Any]:
    if not computer_use_v4_tools_available():
        return _error_result("MODEL_IMAGE_INPUT_UNAVAILABLE")
    try:
        frame = _loop().zoom(frame_id, left, top, right, bottom)
        return _image_result(frame, reason_code="V4_ACTIVE_MODEL_CROP_BUILT")
    except ComputerUseV4Error as exc:
        return _error_result(exc.code, exc.detail)
    except (TypeError, ValueError, OverflowError):
        return _error_result("INVALID_COORDINATES")


def tool_computer_observation(
    frame_id: str,
    status: str,
    observed_state: str,
) -> dict[str, Any]:
    """Report one same-model interpretation of an exact current V4 frame."""

    if not computer_use_v4_tools_available():
        return _error_result("MODEL_IMAGE_INPUT_UNAVAILABLE")
    state = _current_state()
    normalized_status = str(status or "").strip().lower()
    description = " ".join(str(observed_state or "").strip().split())[:512]
    if normalized_status not in {
        "satisfied", "not_satisfied", "inconclusive",
    } or not description:
        return _error_result("INVALID_VISUAL_EVIDENCE")
    loop = _loop()
    frame = loop.frame(str(frame_id or ""))
    if (
        frame is None
        or frame.frame_id != loop.latest_frame_id
        or frame.loop_id != state.loop_id
        or frame.turn_id != state.turn_id
        or frame.provider != state.provider
        or frame.model != state.model
        or time.time() > frame.expires_at
    ):
        return _error_result("STALE_FRAME", "visual_evidence_frame_invalid")

    def _model_report_result() -> dict[str, Any]:
        state.last_result_status = "MODEL_REPORTED_OBSERVATION"
        receipt = {
            "schema_version": 1,
            "dispatch_state": "read_only",
            "outcome": "observed",
            "reason_code": "v4_frame_bound_model_report",
            "v4_loop_id": state.loop_id,
            "frame_id": frame.frame_id,
            "surface_ref": "v4_" + frame.window_token,
            "window_generation": frame.window_token,
            "native_window_id": str(frame.hwnd),
            "captured_at": frame.captured_at,
            "expires_at": frame.expires_at,
            "image_sha256": frame.image_sha256,
            "provider_id": state.provider,
            "model_id": state.model,
            "reported_status": normalized_status,
            "reported_state_sha256": hashlib.sha256(
                description.encode("utf-8", errors="replace")
            ).hexdigest(),
            "authority": "model_report_only",
            "authoritative": False,
            "freshness": "fresh",
        }
        _trace("model_reported_observation", receipt)
        return {
            "text": (
                "This is the selected model's interpretation of the exact "
                "current frame, not independent verification or proof that "
                "a requested physical effect occurred."
            ),
            "receipt": receipt,
        }

    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None:
        # Canonical Chat intentionally has no ExecutionStrategy authority.
        # Keep this read-only capability usable there without converting the
        # model's interpretation into independent evidence or completion.
        return _model_report_result()
    try:
        _attempt, postcondition, checkpoint_role = (
            _current_visual_postcondition()
        )
        if not postcondition:
            return _model_report_result()
        staged = bool(strategy.stage_v4_grounded_observation({
            "conversation_id": state.conversation_id,
            "turn_id": state.turn_id,
            "provider_id": state.provider,
            "model_id": state.model,
            "v4_loop_id": state.loop_id,
            "frame_id": frame.frame_id,
            "surface_ref": "v4_" + frame.window_token,
            "window_generation": frame.window_token,
            "native_window_id": str(frame.hwnd),
            "captured_at": frame.captured_at,
            "expires_at": frame.expires_at,
            "postcondition": postcondition,
            "evidence_purpose": checkpoint_role,
            "status": normalized_status,
            "observed_state": description,
        }))
    except Exception:
        staged = False
        postcondition = ""
    if not staged:
        return _error_result(
            "VISUAL_EVIDENCE_BINDING_FAILED",
            "current_turn_postcondition_binding_unavailable",
        )
    state.last_result_status = "GROUNDED_OBSERVATION"
    receipt = {
        "schema_version": 1,
        "dispatch_state": "read_only",
        "outcome": "observed",
        "reason_code": "v4_grounded_visual_evidence_staged",
        "v4_loop_id": state.loop_id,
        "frame_id": frame.frame_id,
        "surface_ref": "v4_" + frame.window_token,
        "window_generation": frame.window_token,
        "native_window_id": str(frame.hwnd),
        "captured_at": frame.captured_at,
        "expires_at": frame.expires_at,
        "provider_id": state.provider,
        "model_id": state.model,
        "postcondition": postcondition,
        "evidence_purpose": checkpoint_role,
        "evidence_status": normalized_status,
        "observed_state_ref": "sha256:" + hashlib.sha256(
            description.encode("utf-8", errors="replace")
        ).hexdigest(),
        "freshness": "fresh",
    }
    _trace("grounded_observation", receipt)
    return {
        "text": (
            "The exact current-frame visual evidence was staged for "
            "turn-bound verification. Continue truthfully; repeating "
            "an action is allowed."
        ),
        "receipt": receipt,
    }


def _before_action(tool_name: str, arguments: Mapping[str, Any]) -> None:
    hook = _active_config().get("before_action")
    if callable(hook):
        hook(tool_name, dict(arguments))


def prepare_controlled_computer_use_v4_handoff(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    """Run fixture-only setup before the durable owner wait begins."""

    hook = _active_config().get("before_action_handoff")
    if callable(hook):
        hook(str(tool_name or ""), dict(arguments))


def _action_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    state = _current_state()
    status = str(result.get("status") or "ACTION_FAILED")
    dispatched = int(result.get("dispatch_count") or 0) > 0
    verified = status == "DISPATCHED_VERIFIED"
    if status == "STALE_FRAME":
        state.stale_rejection_count += 1
    if dispatched:
        state.callback_count += int(result.get("dispatch_count") or 0)
    state.last_result_status = status
    return {
        "schema_version": 1,
        "dispatch_state": (
            "effect_confirmed" if verified else
            "effect_unknown" if dispatched else "not_dispatched"
        ),
        "outcome": (
            "confirmed_success" if verified else
            "outcome_unknown" if dispatched else
            "confirmed_failure_before_effect"
        ),
        "reason_code": str(result.get("reason_code") or status)[:120],
        "v4_status": status,
        "v4_loop_id": state.loop_id,
        "frame_id": str(result.get("frame_id") or ""),
        "action_kind": str(result.get("action_kind") or ""),
        "action_id": str(result.get("action_id") or ""),
        "attempt_id": str(result.get("attempt_id") or ""),
        "semantic_effect_id": str(result.get("semantic_effect_id") or ""),
        "callback_occurred": dispatched,
        "callback_count": state.callback_count,
        "observation_count": state.observation_count,
        "action_count": state.action_count,
        "stale_rejection_count": state.stale_rejection_count,
        "foreground_preparation": dict(
            result.get("foreground_preparation") or {}
        ),
    }
    _trace("action", receipt)
    return receipt


def _wrap_action(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": str(result.get("text") or "ERROR: ACTION_FAILED"),
        "receipt": _action_receipt(result),
    }


def _execute_action(tool_name: str, action: ComputerAction) -> dict[str, Any]:
    _before_action(tool_name, action.safe_arguments())
    try:
        return _wrap_action(_loop().execute(action))
    except (TypeError, ValueError, OverflowError):
        return _error_result("INVALID_COORDINATES")


def tool_computer_click(
    frame_id: str,
    x: float,
    y: float,
    intent: str = "",
) -> dict[str, Any]:
    return _execute_action(
        "computer_click",
        ComputerAction(
            "click", frame_id, x=x, y=y, effect_id=str(intent or "")[:160],
        ),
    )


def tool_computer_double_click(
    frame_id: str,
    x: float,
    y: float,
    intent: str = "",
) -> dict[str, Any]:
    return _execute_action(
        "computer_double_click",
        ComputerAction(
            "double_click", frame_id, x=x, y=y,
            effect_id=str(intent or "")[:160],
        ),
    )


def tool_computer_type(
    frame_id: str,
    text: str,
    intent: str = "",
) -> dict[str, Any]:
    return _execute_action(
        "computer_type",
        ComputerAction(
            "type", frame_id, text=text, effect_id=str(intent or "")[:160],
        ),
    )


def tool_computer_keypress(
    frame_id: str,
    key: str,
    intent: str = "",
    observe_after: bool = True,
) -> dict[str, Any]:
    return _execute_action(
        "computer_keypress",
        ComputerAction(
            "keypress", frame_id, key=key,
            effect_id=str(intent or "")[:160],
            observe_after=bool(observe_after),
        ),
    )


def tool_computer_scroll(
    frame_id: str,
    direction: str = "down",
    amount: int = 3,
    intent: str = "",
    observe_after: bool = True,
) -> dict[str, Any]:
    return _execute_action(
        "computer_scroll",
        ComputerAction(
            "scroll", frame_id, direction=direction, amount=amount,
            effect_id=str(intent or "")[:160],
            observe_after=bool(observe_after),
        ),
    )


def tool_computer_actions(actions: list[dict]) -> dict[str, Any]:
    if not isinstance(actions, list):
        return _error_result("INVALID_ARGUMENTS")
    try:
        parsed_actions = [
            item if isinstance(item, ComputerAction)
            else ComputerAction.from_mapping(item)
            for item in actions
        ]
        _before_action("computer_actions", {
            "action_count": len(parsed_actions),
            "actions": [item.safe_arguments() for item in parsed_actions],
        })
        result = _loop().execute_batch(parsed_actions)
    except (TypeError, ValueError, OverflowError):
        return _error_result("INVALID_ARGUMENTS")
    results = [
        item for item in result.get("results", ()) if isinstance(item, Mapping)
    ]
    final = results[-1] if results else {
        "status": result.get("status"),
        "reason_code": result.get("reason_code"),
    }
    receipt = _action_receipt({
        **dict(final),
        "dispatch_count": int(result.get("executed_count") or 0),
    })
    receipt["batch_action_count"] = len(actions)
    receipt["batch_executed_count"] = int(result.get("executed_count") or 0)
    status = str(result.get("status") or "ACTION_FAILED")
    if status == "DISPATCHED_VERIFIED":
        text = (
            "Computer Use V4 completed the bounded action batch with an "
            "authoritative effect receipt. Capture a fresh screenshot now."
        )
    elif status == "DISPATCHED":
        text = (
            "ERROR: Computer Use V4 dispatched the bounded action batch once; "
            "its final effect requires a fresh screenshot."
        )
    else:
        text = f"ERROR: Computer Use V4 {status}: {result.get('reason_code') or status}."
    return {"text": text, "receipt": receipt}


def computer_use_v4_action_target(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    if str(tool_name or "") not in COMPUTER_USE_V4_MUTATION_TOOLS:
        return {}
    frame_id = str(arguments.get("frame_id") or "")
    if tool_name == "computer_actions":
        actions = arguments.get("actions")
        first = actions[0] if isinstance(actions, list) and actions else {}
        frame_id = str(first.get("frame_id") or "") if isinstance(first, Mapping) else ""
    state = _TURN_STATE.get()
    frame = state.loop.frame(frame_id) if state is not None and state.loop else None
    return {
        "kind": "desktop",
        "frame_id": frame_id,
        "surface_ref": "v4_" + str(frame.window_token if frame else "unknown"),
        "application": "v4_" + str(frame.window_token if frame else "unknown"),
        "window_generation": str(frame.window_token if frame else ""),
        "native_window_id": str(frame.hwnd if frame else ""),
        "conflict_scope": "v4_" + str(frame.window_token if frame else "unknown"),
    }


def computer_use_v4_state_snapshot() -> dict[str, Any]:
    state = _TURN_STATE.get()
    if state is None:
        return {}
    frame = state.loop.frame(state.loop.latest_frame_id) if state.loop else None
    return {
        "loop_id": state.loop_id,
        "latest_frame_id": str(frame.frame_id if frame else ""),
        "latest_frame_hash": str(frame.image_sha256 if frame else ""),
        "observation_count": state.observation_count,
        "action_count": state.action_count,
        "callback_count": state.callback_count,
        "stale_rejection_count": state.stale_rejection_count,
        "last_result_status": state.last_result_status,
        "capture_lifecycle": (
            state.loop.capture_lifecycle_snapshot() if state.loop else {}
        ),
    }


def trace_computer_use_v4_validation_failure(
    tool_name: str,
    details: Mapping[str, Any],
) -> None:
    _trace("validation_failed", {
        "tool_name": str(tool_name or "")[:80],
        "reason_code": str(details.get("reason") or "invalid_arguments")[:80],
        "issue_paths": [
            {
                "path": str(item.get("path") or "")[:120],
                "code": str(item.get("code") or "")[:80],
            }
            for item in (details.get("issues") or ())[:16]
            if isinstance(item, Mapping)
        ],
    })


def trace_computer_use_v4_model_call(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    _trace("model_action_requested", {
        "tool_name": str(tool_name or "")[:80],
        "argument_fields": sorted(str(key)[:80] for key in arguments),
        "frame_id": str(arguments.get("frame_id") or "")[:160],
    })


def trace_computer_use_v4_preflight(
    tool_name: str,
    *,
    allowed: bool,
    reason_code: str,
) -> None:
    _trace("strategy_preflight", {
        "tool_name": str(tool_name or "")[:80],
        "allowed": bool(allowed),
        "reason_code": str(reason_code or "")[:120],
    })


def trace_computer_use_v4_execution_record(
    tool_name: str,
    execution_record,
) -> None:
    _trace("execution_record", {
        "tool_name": str(tool_name or "")[:80],
        "outcome": str(
            getattr(
                getattr(execution_record, "outcome", None),
                "value",
                getattr(execution_record, "outcome", ""),
            ) or ""
        )[:80],
        "reason_code": str(
            getattr(execution_record, "reason_code", "") or ""
        )[:120],
        "attempt_id": str(
            getattr(execution_record, "tool_attempt_id", "") or ""
        )[:160],
        "dispatched": bool(getattr(execution_record, "dispatched", False)),
    })


__all__ = [
    "COMPUTER_USE_V4_MUTATION_TOOLS",
    "COMPUTER_USE_V4_READ_TOOLS",
    "COMPUTER_USE_V4_TOOL_NAMES",
    "computer_use_v4_action_target",
    "computer_use_v4_state_snapshot",
    "computer_use_v4_test_scope",
    "computer_use_v4_tools_available",
    "computer_use_v4_turn_scope",
    "current_computer_use_v4_state",
    "prepare_controlled_computer_use_v4_handoff",
    "tool_computer_actions",
    "tool_computer_click",
    "tool_computer_double_click",
    "tool_computer_keypress",
    "tool_computer_observation",
    "tool_computer_screenshot",
    "tool_computer_scroll",
    "tool_computer_type",
    "tool_computer_zoom",
    "trace_computer_use_v4_validation_failure",
    "trace_computer_use_v4_model_call",
    "trace_computer_use_v4_preflight",
    "trace_computer_use_v4_execution_record",
]
