"""Generic semantic observation and bounded capture for native desktop apps."""

from __future__ import annotations

import contextvars
import hashlib
import io
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from osia.desktop_surface import (
    DESKTOP_SURFACE_REGISTRY,
    DesktopSurfaceRef,
    NativeSurfaceIdentity,
    SurfaceBounds,
    SurfaceKind,
)
from osia.desktop_vision_policy import (
    AccessibilityQuality,
    DesktopEvidence,
    classify_accessibility_quality,
    decide_visual_observation,
)
from osia.image_inputs import image_input_decision
from osia.visual_observation import (
    CaptureKind,
    ObservationReason,
    VisualObservationStore,
)


_MAX_UIA_NODES = 320
_MAX_MODEL_SURFACES = 80
_ACTIONABLE_PATTERNS = frozenset({
    "ExpandCollapse", "Invoke", "SelectionItem", "Toggle", "Value",
})
_BACKGROUND_SAFE_UIA_PATTERNS = frozenset({
    "ExpandCollapse", "Invoke", "SelectionItem", "Toggle",
})
_UIA_ACTION_PATTERN_METHODS = (
    ("Invoke", "GetInvokePattern", "Invoke"),
    ("SelectionItem", "GetSelectionItemPattern", "Select"),
    ("Toggle", "GetTogglePattern", "Toggle"),
    ("ExpandCollapse", "GetExpandCollapsePattern", "Expand"),
)
_PRIMITIVE_STATE_ROLES = frozenset({
    "DocumentControl", "EditControl", "PaneControl", "TextControl",
})
_MAX_PRIMITIVE_STATE_ROWS = 32
_PRIMITIVE_ABSENCE_SETTLE_SECONDS = 5.0
_UIA_ACTION_DISPATCH_TIMEOUT_SECONDS = 0.75
_UIA_ACTION_CANCEL_SETTLE_SECONDS = 0.1
_SECURE_DESKTOP_PROCESS_NAMES = frozenset({
    "consent", "credentialuibroker", "lockapp", "logonui", "winlogon",
})
_SCENE_STORES: contextvars.ContextVar[VisualObservationStore | None] = (
    contextvars.ContextVar("osia_visual_observation_store", default=None)
)
_MODEL_ROUTE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "osia_desktop_model_route", default=None,
)
_LAST_SCENE: contextvars.ContextVar["DesktopScene | None"] = contextvars.ContextVar(
    "osia_last_desktop_scene", default=None,
)


@dataclass(frozen=True)
class DesktopScene:
    application_id: str
    window_ref: DesktopSurfaceRef
    surfaces: tuple[DesktopSurfaceRef, ...]
    structural_epoch: int
    scene_signature: str
    accessibility_complete: bool
    observed_at: float
    uia_tree_observed: bool = True
    foreground: bool = True
    host_application_id: str = ""
    content_application_id: str = ""
    packaged_application_id: str = ""
    minimized: bool = False

    def target_evidence(
        self,
        target: str = "",
        *,
        requested: DesktopSurfaceRef | None = None,
        scene_changed: bool = False,
    ) -> DesktopEvidence:
        candidates = rank_surface_candidates(self.surfaces, target)
        actionable_patterns = sum(
            bool(_ACTIONABLE_PATTERNS.intersection(item.patterns))
            for item in self.surfaces
            if item.surface_kind is not SurfaceKind.WINDOW
        )
        actionable_candidates = sum(
            bool(
                item.enabled
                and _ACTIONABLE_PATTERNS.intersection(item.patterns)
            )
            for item in candidates
        )
        return DesktopEvidence(
            candidate_count=len(candidates),
            actionable_candidate_count=actionable_candidates,
            accessible_node_count=max(0, len(self.surfaces) - 1),
            custom_rendered=bool(
                len(self.surfaces) > 1 and actionable_patterns == 0
            ),
            popup_suspected=any(
                item.surface_kind is SurfaceKind.DIALOG
                for item in self.surfaces
            ),
            small_target=bool(
                requested
                and requested.bounds
                and (
                    requested.bounds.width < 48
                    or requested.bounds.height < 28
                )
            ),
            local_region_available=bool(
                (requested and requested.bounds) or self.window_ref.bounds
            ),
            scene_changed=bool(scene_changed),
            target_requested=bool(str(target or "").strip()),
            actionable_pattern_count=actionable_patterns,
            focus_observable=any(
                item.focused and item.surface_kind is not SurfaceKind.WINDOW
                for item in self.surfaces
            ),
            tree_observed=self.uia_tree_observed,
            scene_window_exact=True,
        )

    def accessibility_quality(self, target: str = "") -> AccessibilityQuality:
        return classify_accessibility_quality(self.target_evidence(target))

    def safe_dict(self, *, target: str = "") -> dict:
        candidates = rank_surface_candidates(self.surfaces, target)
        evidence = self.target_evidence(target)
        quality = classify_accessibility_quality(evidence)
        by_ref = {item.ref_id: item for item in self.surfaces}
        projected: list[DesktopSurfaceRef] = []
        projected_refs: set[str] = set()

        def include_with_ancestors(item: DesktopSurfaceRef) -> None:
            parent = by_ref.get(item.parent_ref or "")
            if parent is not None and parent.ref_id not in projected_refs:
                include_with_ancestors(parent)
            if (
                item.ref_id not in projected_refs
                and len(projected) < _MAX_MODEL_SURFACES
            ):
                projected.append(item)
                projected_refs.add(item.ref_id)

        include_with_ancestors(self.window_ref)
        for item in candidates[:12]:
            include_with_ancestors(item)
        for item in self.surfaces:
            if item.selected or item.focused or item.surface_kind is SurfaceKind.DIALOG:
                include_with_ancestors(item)
        for item in self.surfaces:
            include_with_ancestors(item)
        return {
            "application": self.application_id,
            "host_application": self.host_application_id or self.application_id,
            "content_application": self.content_application_id,
            "window_surface_ref": self.window_ref.ref_id,
            "window_generation": self.window_ref.window_generation,
            "target_foreground": bool(self.foreground),
            "target_minimized": bool(self.minimized),
            "structural_epoch": self.structural_epoch,
            "accessibility_complete": self.accessibility_complete,
            "accessibility_quality": quality.value,
            "target_candidate_count": evidence.candidate_count,
            "target_actionable_candidate_count": (
                evidence.actionable_candidate_count
            ),
            "focus_observable": evidence.focus_observable,
            "observed_at": round(self.observed_at, 3),
            "selected_views": [
                item.ref_id for item in self.surfaces
                if item.selected and item.surface_kind in {
                    SurfaceKind.TAB,
                    SurfaceKind.PANE,
                    SurfaceKind.CONVERSATION,
                    SurfaceKind.LIST,
                }
            ],
            "focused_surface_ref": next(
                (
                    item.ref_id for item in self.surfaces
                    if item.focused and item.surface_kind is not SurfaceKind.WINDOW
                ),
                self.window_ref.ref_id if self.window_ref.focused else None,
            ),
            "modal_surface_refs": [
                item.ref_id for item in self.surfaces
                if item.surface_kind is SurfaceKind.DIALOG
            ],
            "candidate_surface_refs": [item.ref_id for item in candidates[:12]],
            "surfaces": [
                {
                    **item.to_safe_dict(),
                    "actuation": _surface_actuation_contract(
                        item,
                        target_foreground=self.foreground,
                        target_minimized=self.minimized,
                    ),
                }
                for item in projected
            ],
        }


def _surface_actuation_contract(
    surface: DesktopSurfaceRef,
    *,
    target_foreground: bool,
    target_minimized: bool,
) -> dict:
    """Describe exact per-surface preconditions without application heuristics."""

    if surface.surface_kind in {SurfaceKind.WINDOW, SurfaceKind.DIALOG}:
        return {
            "available": True,
            "attempted_effect": "application_foreground",
            "preconditions": ["exact_target_exists", "target_restorable"],
            "background_safe": False,
            "foreground_required": False,
            "keyboard_focus_required": False,
        }
    safe_patterns = tuple(
        item for item in (
            "Invoke", "SelectionItem", "Toggle", "ExpandCollapse",
        )
        if item in surface.patterns
    )
    if safe_patterns and surface.enabled and not target_minimized:
        return {
            "available": True,
            "attempted_effect": "desktop_uia_primitive",
            "preconditions": [
                "exact_target_exists", "target_visible_not_minimized",
                "fresh_runtime_identity",
            ],
            "background_safe": True,
            "foreground_required": False,
            "keyboard_focus_required": False,
            "patterns": list(safe_patterns),
        }
    return {
        "available": False,
        "attempted_effect": "",
        "preconditions": [
            "exact_target_exists",
            *([] if target_foreground else ["target_foreground"]),
            *([] if not target_minimized else ["target_restored"]),
        ],
        "background_safe": False,
        "foreground_required": not target_foreground,
        "keyboard_focus_required": False,
        "reason_code": "safe_uia_pattern_unavailable",
    }


def bind_effective_model(provider: str, model: str):
    """Bind the exact route whose next provider request may receive an image."""

    return _MODEL_ROUTE.set({
        "provider": str(provider or "").lower(),
        "model": str(model or ""),
    })


def reset_effective_model(token) -> None:
    _MODEL_ROUTE.reset(token)


def begin_desktop_turn() -> tuple[object, object]:
    return (
        _SCENE_STORES.set(VisualObservationStore()),
        _LAST_SCENE.set(None),
    )


def end_desktop_turn(tokens: tuple[object, object]) -> None:
    store = _SCENE_STORES.get()
    if store is not None:
        store.clear()
    store_token, scene_token = tokens
    _LAST_SCENE.reset(scene_token)
    _SCENE_STORES.reset(store_token)


def effective_model_supports_vision() -> bool:
    route = _MODEL_ROUTE.get() or {}
    return image_input_decision(
        route.get("provider", ""),
        route.get("model", ""),
        ("image/png",),
    ).usable


def effective_model_image_route() -> tuple[str, str]:
    """Return the exact bound route; never resolve a companion Vision model."""

    route = _MODEL_ROUTE.get() or {}
    provider = str(route.get("provider") or "").lower()
    model = str(route.get("model") or "")
    decision = image_input_decision(provider, model, ("image/png",))
    return (provider, model) if decision.usable else ("", "")


def _safe_label(value: object, *, password: bool = False) -> str:
    if password:
        return "[sensitive field]"
    label = " ".join(str(value or "").split())
    return label[:240]


def _runtime_id(control) -> tuple[int, ...]:
    try:
        value = control.GetRuntimeId()
    except Exception:
        value = ()
    try:
        return tuple(int(item) for item in (value or ()))[:32]
    except Exception:
        return ()


def _runtime_signature(
    runtime_id: tuple[int, ...],
    role: str,
    ancestor: str,
    automation_id: str = "",
) -> str:
    if not runtime_id and not automation_id:
        return ""
    identity = f"automation:{automation_id}" if automation_id else repr(runtime_id)
    material = f"{identity}\x1f{role}\x1f{ancestor}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _process_details(pid: int) -> tuple[str, float]:
    try:
        import psutil

        process = psutil.Process(int(pid))
        return Path(process.name()).stem.lower(), float(process.create_time())
    except Exception:
        # Without a trustworthy process birth time, never claim stability across
        # observations: a fresh nonce-like timestamp makes prior refs stale.
        return f"process-{int(pid or 0)}", time.time()


def _semantic_application_id(
    window_application_id: str,
    window_pid: int,
    controls: Iterable[dict],
) -> str:
    """Resolve the real app behind the generic Windows UWP frame host."""

    if str(window_application_id or "").lower() != "applicationframehost":
        return str(window_application_id or "")
    process_counts: dict[int, int] = {}
    for item in controls:
        try:
            process_id = int(item.get("process_id") or 0)
        except (TypeError, ValueError):
            continue
        if process_id <= 0 or process_id == int(window_pid):
            continue
        process_counts[process_id] = process_counts.get(process_id, 0) + 1
    for process_id, _count in sorted(
        process_counts.items(),
        key=lambda item: (-item[1], item[0]),
    ):
        candidate, _started_at = _process_details(process_id)
        if candidate and not candidate.startswith("process-"):
            return candidate
    return str(window_application_id or "")


def _application_identity(value: object) -> str:
    normalized = "".join(
        char for char in str(value or "").lower()
        if char.isalnum()
    )
    for suffix in ("application", "app", "exe"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 2:
            normalized = normalized[:-len(suffix)]
            break
    return normalized


def _application_words(value: object) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _scene_application_identity(
    scene: DesktopScene,
    expected: str,
) -> tuple[int, str]:
    """Return one structured target-identity rank and its bounded evidence."""

    target = _application_identity(expected)
    observed = _application_identity(scene.application_id)
    host = _application_identity(
        scene.host_application_id or scene.application_id,
    )
    if not target:
        return 0, ""
    content = _application_identity(scene.content_application_id)
    if host == "applicationframehost" and content == target:
        return 400, f"uia_content_application:{scene.content_application_id}"
    packaged_application_id = str(scene.packaged_application_id or "")
    if packaged_application_id:
        try:
            from osia.tools import desktop_control as dc

            packaged_match = bool(
                dc._packaged_app_identity_score(expected, packaged_application_id)
            )
        except Exception:
            packaged_match = False
        if packaged_match:
            return 350, (
                "packaged_application_id:"
                + _safe_label(packaged_application_id)
            )
    if observed == target:
        if host == "applicationframehost" and observed != host:
            return 400, f"uia_content_application:{scene.application_id}"
        return 400, f"executable_application:{scene.application_id}"
    # A generic UWP frame is never application identity by itself.  Only the
    # content-process identity projected above can turn it into an exact app.
    if host == "applicationframehost" or observed == "applicationframehost":
        return 0, ""
    title = scene.window_ref.accessible_label
    if _application_identity(title) == target:
        return 300, f"exact_window_title:{_safe_label(title)}"
    expected_words = _application_words(expected)
    title_words = _application_words(title)
    if not expected_words or len(expected_words) > len(title_words):
        return 0, ""
    width = len(expected_words)
    if any(
        title_words[index:index + width] == expected_words
        for index in range(len(title_words) - width + 1)
    ):
        return 200, f"structured_window_title:{_safe_label(title)}"
    return 0, ""


def scene_matches_application(scene: DesktopScene, expected: str) -> bool:
    """Match one scene to the intended app without trusting PID alone."""

    rank, _evidence = _scene_application_identity(scene, expected)
    return rank > 0


def _ensure_thread_physical_pixels() -> None:
    """Use the same physical-pixel coordinate space as UIA and ImageGrab."""

    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.SetThreadDpiAwarenessContext(
            ctypes.c_void_p(-4),  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except Exception:
        pass


def _window_bounds(hwnd: object) -> SurfaceBounds | None:
    if sys.platform != "win32":
        return None
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return None
        win32gui, _win32process = loaded
        if win32gui.IsIconic(int(hwnd)):
            return None
        left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
        dpi_scale = 1.0
        try:
            import ctypes

            dpi_scale = max(
                1.0,
                float(ctypes.windll.user32.GetDpiForWindow(int(hwnd))) / 96.0,
            )
        except Exception:
            pass
        bounds = SurfaceBounds(
            int(left), int(top), int(right), int(bottom), dpi_scale=dpi_scale,
        )
        return bounds if bounds.valid() else None
    except Exception:
        return None


def _window_is_minimized(hwnd: object) -> bool:
    if sys.platform != "win32":
        return False
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        return bool(loaded and loaded[0].IsIconic(int(hwnd)))
    except Exception:
        return False


def _window_packaged_application_id(hwnd: object) -> str:
    """Return one window-bound stable AUMID, never a localized display name."""

    if sys.platform != "win32":
        return ""
    try:
        from osia.tools import desktop_control as dc
        from win32com.propsys import propsys, pscon

        value = propsys.SHGetPropertyStoreForWindow(int(hwnd)).GetValue(
            pscon.PKEY_AppUserModel_ID
        )
        app_id = str(value.GetValue() if value is not None else "").strip()
        return app_id if dc._safe_apps_folder_id(app_id) else ""
    except Exception:
        return ""


def _window_metadata(hwnd: object) -> tuple[str, int, str, SurfaceBounds | None]:
    if sys.platform != "win32":
        return "", 0, "", None
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        if not loaded:
            return "", 0, "", None
        win32gui, win32process = loaded
        title = str(win32gui.GetWindowText(int(hwnd)) or "")
        class_name = str(win32gui.GetClassName(int(hwnd)) or "")
        _thread, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        return title, int(pid or 0), class_name, _window_bounds(hwnd)
    except Exception:
        return "", 0, "", None


def _owner_window_id(hwnd: object) -> int:
    if sys.platform != "win32":
        return 0
    try:
        from osia.tools import desktop_control as dc

        loaded = dc._load_win32()
        return int(loaded[0].GetWindow(int(hwnd), 4) or 0) if loaded else 0
    except Exception:
        return 0


def _control_bounds(control, window_bounds: SurfaceBounds) -> SurfaceBounds | None:
    try:
        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
        bounds = SurfaceBounds(
            left,
            top,
            right,
            bottom,
            left - window_bounds.left,
            top - window_bounds.top,
            right - window_bounds.left,
            bottom - window_bounds.top,
            dpi_scale=window_bounds.dpi_scale,
        )
        return bounds if bounds.valid() else None
    except Exception:
        return None


def _bool_property(control, name: str, default: bool = False) -> bool:
    try:
        value = getattr(control, name)
        return bool(value() if callable(value) else value)
    except Exception:
        return bool(default)


def _selection_state(control) -> bool:
    if _bool_property(control, "IsSelected", False):
        return True
    try:
        pattern = control.GetSelectionItemPattern()
        return bool(pattern and pattern.IsSelected)
    except Exception:
        return False


def _supported_patterns(control) -> tuple[str, ...]:
    """Expose only pattern names, never Value/Text/password contents."""

    supported = []
    action_methods = {
        pattern_name: action_method
        for pattern_name, _getter_name, action_method
        in _UIA_ACTION_PATTERN_METHODS
    }
    for safe_name, method_name in (
        ("Selection", "GetSelectionPattern"),
        ("SelectionItem", "GetSelectionItemPattern"),
        ("Invoke", "GetInvokePattern"),
        ("Value", "GetValuePattern"),
        ("Text", "GetTextPattern"),
        ("ExpandCollapse", "GetExpandCollapsePattern"),
        ("Toggle", "GetTogglePattern"),
        ("Window", "GetWindowPattern"),
    ):
        try:
            getter = getattr(control, method_name, None)
            pattern = getter() if callable(getter) else None
            if pattern is None:
                continue
            action_method = action_methods.get(safe_name)
            if action_method and not callable(getattr(pattern, action_method, None)):
                continue
            supported.append(safe_name)
        except Exception:
            continue
    return tuple(supported)


def _fresh_uia_action_methods(control) -> dict[str, object]:
    """Return only canonical UIA actions callable on this fresh reference."""

    available = {}
    for pattern_name, getter_name, method_name in _UIA_ACTION_PATTERN_METHODS:
        try:
            getter = getattr(control, getter_name, None)
            pattern = getter() if callable(getter) else None
            method = getattr(pattern, method_name, None) if pattern else None
        except Exception:
            continue
        if callable(method):
            available[pattern_name] = method
    return available


def _dispatch_fresh_uia_action_bounded(
    auto,
    *,
    native_window_id: int,
    runtime_id: tuple[int, ...],
    action_pattern: str,
    observed_patterns: Iterable[str],
    invoke_preflight_reason: str = "",
    journal_dispatch_started=None,
    journal_dispatch_returned=None,
) -> dict:
    """Bound one UIA provider call while preserving its exact dispatch state."""

    done = threading.Event()
    cancel_before_dispatch = threading.Event()
    dispatch_gate = threading.Lock()
    state = {
        "dispatch_started": False,
        "dispatched_at": 0.0,
        "completed": False,
        "reason_code": "",
        "worker_may_be_active": False,
        "worker_native_id": 0,
        "automation_id": "",
        "fresh_supported_patterns": [],
        "selected_pattern": action_pattern,
        "selected_pattern_available": False,
    }

    def _worker() -> None:
        cancellation_enabled = False
        try:
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(int(native_window_id))
                control = _find_runtime_control(root, runtime_id)
                if control is None:
                    state["reason_code"] = "stale_semantic_reference"
                    return
                try:
                    state["automation_id"] = _safe_label(control.AutomationId)
                except Exception:
                    pass
                fresh_actions = _fresh_uia_action_methods(control)
                state["fresh_supported_patterns"] = list(fresh_actions)
                allowed_patterns = set(observed_patterns)
                selected_pattern = action_pattern or next((
                    pattern_name
                    for pattern_name, _getter_name, _method_name
                    in _UIA_ACTION_PATTERN_METHODS
                    if pattern_name in fresh_actions
                    and pattern_name in allowed_patterns
                ), "")
                state["selected_pattern"] = selected_pattern
                method = (
                    fresh_actions.get(selected_pattern)
                    if selected_pattern in allowed_patterns else
                    None
                )
                state["selected_pattern_available"] = callable(method)
                if not callable(method):
                    state["reason_code"] = "uia_action_pattern_unavailable"
                    return
                if selected_pattern == "Invoke" and invoke_preflight_reason:
                    state["reason_code"] = invoke_preflight_reason
                    return
                with dispatch_gate:
                    if cancel_before_dispatch.is_set():
                        state["reason_code"] = "uia_action_dispatch_timed_out_before_effect"
                        return
                    if (
                        callable(journal_dispatch_started)
                        and not journal_dispatch_started(
                            automation_id=state["automation_id"],
                            uia_pattern=selected_pattern,
                        )
                    ):
                        state["reason_code"] = "primitive_journal_unavailable"
                        return
                    state["dispatched_at"] = time.time()
                    state["dispatch_started"] = True
                    state["worker_native_id"] = threading.get_native_id()
                try:
                    import ctypes

                    cancellation_enabled = (
                        ctypes.windll.ole32.CoEnableCallCancellation(None) >= 0
                    )
                except Exception:
                    cancellation_enabled = False
                method()
                if (
                    callable(journal_dispatch_returned)
                    and not journal_dispatch_returned()
                ):
                    state["reason_code"] = (
                        "primitive_journal_return_unavailable"
                    )
                    return
                state["completed"] = True
        except Exception:
            state["reason_code"] = (
                "uia_activation_outcome_unknown"
                if state["dispatch_started"] else
                "native_accessibility_invocation_unavailable"
            )
        finally:
            if cancellation_enabled:
                try:
                    import ctypes

                    ctypes.windll.ole32.CoDisableCallCancellation(None)
                except Exception:
                    pass
            done.set()

    worker = threading.Thread(
        target=_worker,
        name="osia-uia-action",
        daemon=True,
    )
    worker.start()
    if not done.wait(_UIA_ACTION_DISPATCH_TIMEOUT_SECONDS):
        with dispatch_gate:
            if not state["dispatch_started"]:
                cancel_before_dispatch.set()
                state["reason_code"] = (
                    "uia_action_dispatch_timed_out_before_effect"
                )
            else:
                state["reason_code"] = "uia_action_dispatch_timed_out"
                try:
                    import ctypes

                    ctypes.windll.ole32.CoCancelCall(
                        int(state["worker_native_id"]),
                        0,
                    )
                except Exception:
                    pass
        done.wait(_UIA_ACTION_CANCEL_SETTLE_SECONDS)
        state["worker_may_be_active"] = not done.is_set()
    return dict(state)


def _durable_primitive_journal_callbacks(
    *,
    application_identity: str,
    surface_ref: str,
    native_window_id: object,
    window_generation: str,
    runtime_identity: str,
):
    """Bind the current durable attempt without creating another state store."""

    try:
        from osia.tool_attempts import (
            current_tool_attempt_id,
            current_tool_attempt_store,
        )

        attempt_id = current_tool_attempt_id()
        store = current_tool_attempt_store()
    except Exception:
        attempt_id = ""
        store = None
    if not attempt_id:
        return None, None
    if store is None:
        return (lambda **_kwargs: False), (lambda: False)
    try:
        durable = store.get_attempt(attempt_id) or {}
        journal = list(durable.get("primitive_journal") or [])
        prepared = next((
            item for item in journal
            if item.get("lifecycle") == "prepared"
            and (
                item.get("surface_ref") == str(surface_ref or "")
                or (
                    runtime_identity
                    and item.get("runtime_identity") == runtime_identity
                )
            )
        ), None)
        if prepared is None and len(journal) == 1:
            prepared = journal[0]
        primitive_id = str(
            prepared.get("primitive_id") if isinstance(prepared, dict) else ""
        )
    except Exception:
        primitive_id = ""

    def _started(*, automation_id: str = "", uia_pattern: str = "") -> bool:
        if not primitive_id:
            return False
        try:
            updated = store.record_primitive_dispatch_started(
                attempt_id,
                primitive_id=primitive_id,
                application_identity=application_identity,
                surface_ref=surface_ref,
                native_window_id=str(native_window_id or ""),
                window_generation=window_generation,
                runtime_identity=runtime_identity,
                automation_id=automation_id,
                uia_pattern=uia_pattern,
            )
            entry = next(
                (
                    item for item in updated.get("primitive_journal") or []
                    if item.get("primitive_id") == primitive_id
                ),
                {},
            )
            return entry.get("lifecycle") == "dispatch_started"
        except Exception:
            return False

    def _returned() -> bool:
        if not primitive_id:
            return False
        try:
            updated = store.record_primitive_dispatch_returned(
                attempt_id,
                primitive_id=primitive_id,
            )
            entry = next(
                (
                    item for item in updated.get("primitive_journal") or []
                    if item.get("primitive_id") == primitive_id
                ),
                {},
            )
            return entry.get("lifecycle") == "dispatch_returned"
        except Exception:
            return False

    return _started, _returned


def _surface_kind(role: str, *, modal: bool = False) -> SurfaceKind:
    if modal or role == "WindowControl":
        return SurfaceKind.DIALOG if modal else SurfaceKind.WINDOW
    return {
        "TabControl": SurfaceKind.TAB,
        "TabItemControl": SurfaceKind.TAB,
        "PaneControl": SurfaceKind.PANE,
        "DocumentControl": SurfaceKind.PANE,
        "ListControl": SurfaceKind.LIST,
        "ListItemControl": SurfaceKind.CONVERSATION,
        "TreeControl": SurfaceKind.LIST,
        "TreeItemControl": SurfaceKind.CONVERSATION,
        "ToolBarControl": SurfaceKind.TOOLBAR,
        "GroupControl": SurfaceKind.PANE,
    }.get(role, SurfaceKind.CONTROL)


def _scene_signature(rows: Iterable[tuple]) -> str:
    material = repr(tuple(rows))
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:32]


def _observe_window_scene(
    window: tuple[object, str, int],
    *,
    target: str = "",
    foreground: bool,
    class_name_hint: str = "",
) -> DesktopScene | None:
    """Observe one exact top-level window without exposing its native handle."""

    if sys.platform != "win32":
        return None
    _ensure_thread_physical_pixels()
    from osia.tools import desktop_control as dc

    hwnd, title, pid = window
    if not hwnd or not pid:
        return None
    minimized = _window_is_minimized(hwnd)
    window_bounds = _window_bounds(hwnd)
    if window_bounds is None and not minimized:
        return None
    host_application_id, process_started_at = _process_details(pid)
    packaged_application_id = _window_packaged_application_id(hwnd)
    class_name = str(class_name_hint or "")
    if not class_name:
        try:
            loaded = dc._load_win32()
            class_name = loaded[0].GetClassName(hwnd) if loaded else ""
        except Exception:
            class_name = ""

    rows = []
    controls = []
    root_is_modal = False
    uia_tree_observed = False
    auto = dc._load_uiautomation()
    if auto is not None:
        try:
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(int(hwnd))
                uia_tree_observed = root is not None
                root_is_modal = _bool_property(root, "IsModal", False)
                try:
                    root_children = root.GetChildren() if root else ()
                except Exception:
                    root_children = ()
                root_runtime = _runtime_id(root) if root else ()
                root_ancestor = _runtime_signature(
                    root_runtime, "WindowControl", "window",
                ) or "window"
                frontier = [
                    (child, None, root_ancestor, 1)
                    for child in (root_children or ())
                ]
                while frontier and len(controls) < _MAX_UIA_NODES:
                    control, parent_index, ancestor, depth = frontier.pop(0)
                    try:
                        role = str(control.ControlTypeName or "")
                    except Exception:
                        role = ""
                    try:
                        password = bool(control.IsPassword)
                    except Exception:
                        password = False
                    try:
                        label = _safe_label(control.Name, password=password)
                    except Exception:
                        label = ""
                    try:
                        automation_id = _safe_label(control.AutomationId)
                    except Exception:
                        automation_id = ""
                    try:
                        control_process_id = int(control.ProcessId)
                    except Exception:
                        control_process_id = int(pid)
                    runtime_id = _runtime_id(control)
                    bounds = _control_bounds(control, window_bounds)
                    selected = _selection_state(control)
                    focused = _bool_property(control, "HasKeyboardFocus", False)
                    enabled = _bool_property(control, "IsEnabled", True)
                    modal = _bool_property(control, "IsModal", False)
                    patterns = _supported_patterns(control)
                    index = len(controls)
                    controls.append({
                        "control": control,
                        "parent_index": parent_index,
                        "ancestor": ancestor,
                        "role": role or "CustomControl",
                        "label": label or automation_id,
                        "automation_id": automation_id,
                        "process_id": control_process_id,
                        "runtime_id": runtime_id,
                        "bounds": bounds,
                        "selected": selected,
                        "focused": focused,
                        "enabled": enabled,
                        "modal": modal,
                        "patterns": patterns,
                    })
                    rows.append((
                        runtime_id,
                        role,
                        parent_index,
                        selected if _surface_kind(role, modal=modal) in {
                            SurfaceKind.TAB,
                            SurfaceKind.PANE,
                            SurfaceKind.CONVERSATION,
                        } else False,
                    ))
                    if depth < 12:
                        try:
                            children = control.GetChildren()
                        except Exception:
                            children = ()
                        next_ancestor = _runtime_signature(
                            runtime_id, role, ancestor,
                        ) or ancestor
                        for child in children or ():
                            frontier.append((child, index, next_ancestor, depth + 1))
        except Exception:
            controls = []
            rows = []
            root_is_modal = False
            uia_tree_observed = False

    application_id = _semantic_application_id(
        host_application_id,
        int(pid),
        controls,
    )
    content_application_id = (
        application_id
        if (
            _application_identity(host_application_id) == "applicationframehost"
            and _application_identity(application_id) != "applicationframehost"
        ) else ""
    )
    application_hint = _strategy_required_application() or str(target or "")
    if (
        _application_identity(host_application_id) == "applicationframehost"
        and packaged_application_id
        and application_hint
    ):
        try:
            packaged_match = bool(
                dc._packaged_app_identity_score(
                    application_hint,
                    packaged_application_id,
                )
            )
        except Exception:
            packaged_match = False
        if packaged_match:
            application_id = _application_identity(application_hint)

    signature = _scene_signature(
        rows or [(
            class_name,
            (
                window_bounds.to_safe_dict()
                if window_bounds is not None else {"minimized": True}
            ),
            root_is_modal,
            packaged_application_id,
        )]
    )
    process_ref, generation, epoch = DESKTOP_SURFACE_REGISTRY.observe_window_scene(
        application_id=application_id,
        pid=int(pid),
        process_started_at=process_started_at,
        native_window_id=hwnd,
        class_name=class_name,
        scene_signature=signature,
    )
    native_window = NativeSurfaceIdentity(
        pid=int(pid),
        process_started_at=process_started_at,
        native_window_id=hwnd,
        class_name=class_name,
    )
    owner_ref_id = None
    owner_hwnd = _owner_window_id(hwnd)
    if owner_hwnd:
        owner_title, owner_pid, owner_class, owner_bounds = _window_metadata(owner_hwnd)
        if owner_pid == int(pid) and owner_bounds is not None:
            owner_process_ref, owner_generation, owner_epoch = (
                DESKTOP_SURFACE_REGISTRY.observe_window_scene(
                    application_id=application_id,
                    pid=owner_pid,
                    process_started_at=process_started_at,
                    native_window_id=owner_hwnd,
                    class_name=owner_class,
                    scene_signature=_scene_signature((
                        (owner_class, owner_title, owner_bounds.to_safe_dict()),
                    )),
                )
            )
            owner_ref = DESKTOP_SURFACE_REGISTRY.register(
                application_id=application_id,
                process_ref=owner_process_ref,
                generation=owner_generation,
                surface_kind=SurfaceKind.WINDOW,
                role="WindowControl",
                accessible_label=_safe_label(owner_title),
                selected=False,
                focused=False,
                enabled=True,
                parent_ref=None,
                bounds=owner_bounds,
                runtime_signature="",
                structural_epoch=owner_epoch,
                scene_signature="owner_window",
                native=NativeSurfaceIdentity(
                    pid=owner_pid,
                    process_started_at=process_started_at,
                    native_window_id=owner_hwnd,
                    class_name=owner_class,
                ),
                stable_identity="window",
                patterns=("Window",),
            )
            owner_ref_id = owner_ref.ref_id
    window_ref = DESKTOP_SURFACE_REGISTRY.register(
        application_id=application_id,
        process_ref=process_ref,
        generation=generation,
        surface_kind=(
            SurfaceKind.DIALOG
            if owner_ref_id or root_is_modal
            else SurfaceKind.WINDOW
        ),
        role="WindowControl",
        accessible_label=_safe_label(title),
        selected=bool(foreground),
        focused=bool(foreground),
        enabled=True,
        parent_ref=owner_ref_id,
        bounds=window_bounds,
        runtime_signature="",
        structural_epoch=epoch,
        scene_signature=signature,
        native=native_window,
        stable_identity="window",
        patterns=("Window",),
    )
    refs: list[DesktopSurfaceRef] = [window_ref]
    ref_by_index: dict[int, str] = {}
    for index, item in enumerate(controls):
        runtime_id = item["runtime_id"]
        role = item["role"]
        parent_ref = ref_by_index.get(item["parent_index"], window_ref.ref_id)
        runtime_signature = _runtime_signature(
            runtime_id, role, item["ancestor"], item["automation_id"],
        )
        bounds = item["bounds"]
        stable_identity = runtime_signature or hashlib.sha256(
            repr((role, item["automation_id"], item["label"], bounds)).encode(
                "utf-8", errors="replace",
            )
        ).hexdigest()[:24]
        native = NativeSurfaceIdentity(
            pid=int(pid),
            process_started_at=process_started_at,
            native_window_id=hwnd,
            runtime_id=runtime_id,
            ancestor_signature=item["ancestor"],
            class_name=class_name,
        )
        ref = DESKTOP_SURFACE_REGISTRY.register(
            application_id=application_id,
            process_ref=process_ref,
            generation=generation,
            surface_kind=_surface_kind(role, modal=item["modal"]),
            role=role,
            accessible_label=item["label"],
            selected=item["selected"],
            focused=bool(foreground and item["focused"]),
            enabled=item["enabled"],
            parent_ref=parent_ref,
            bounds=bounds,
            runtime_signature=runtime_signature,
            structural_epoch=epoch,
            scene_signature=signature,
            native=native,
            stable_identity=stable_identity,
            patterns=item["patterns"],
            automation_id=item["automation_id"],
        )
        refs.append(ref)
        ref_by_index[index] = ref.ref_id
    scene = DesktopScene(
        application_id=application_id,
        window_ref=window_ref,
        surfaces=tuple(refs),
        structural_epoch=epoch,
        scene_signature=signature,
        accessibility_complete=sum(
            bool(item["label"] or item["patterns"])
            and item["role"] in {
                "ButtonControl", "CheckBoxControl", "ComboBoxControl",
                "EditControl", "HyperlinkControl", "ListControl",
                "ListItemControl", "MenuControl", "MenuItemControl",
                "RadioButtonControl", "TabControl", "TabItemControl",
                "TreeControl", "TreeItemControl",
            }
            for item in controls
        ) >= 2,
        observed_at=time.time(),
        uia_tree_observed=uia_tree_observed,
        foreground=bool(foreground),
        host_application_id=host_application_id,
        content_application_id=content_application_id,
        packaged_application_id=packaged_application_id,
        minimized=bool(minimized),
    )
    return scene


def observe_foreground_scene(*, target: str = "") -> DesktopScene | None:
    """Observe one exact foreground window without exposing its native handle."""

    if sys.platform != "win32":
        return None
    from osia.tools import desktop_control as dc

    foreground = dc._foreground_window_win32()
    if not foreground:
        return None
    scene = _observe_window_scene(
        foreground,
        target=target,
        foreground=True,
    )
    if scene is not None:
        _LAST_SCENE.set(scene)
    return scene


@dataclass(frozen=True)
class TargetSurfaceSelection:
    requested_application: str
    status: str
    foreground_scene: DesktopScene | None
    target_scene: DesktopScene | None
    identity_strength: int = 0
    identity_evidence: str = ""
    candidate_count: int = 0
    top_level_surfaces: tuple[dict, ...] = ()


@dataclass(frozen=True)
class _LaunchTargetCorrelation:
    application_id: str
    process_ref: str
    window_generation: str
    correlated_at: float


_LAUNCH_TARGET_TTL_SECONDS = 90.0
_LAUNCH_TARGETS: dict[str, _LaunchTargetCorrelation] = {}
_LAUNCH_TARGETS_LOCK = threading.Lock()


def _remember_launch_target(scene: DesktopScene) -> None:
    application = _application_identity(scene.application_id)
    if not application:
        return
    correlation = _LaunchTargetCorrelation(
        application_id=application,
        process_ref=scene.window_ref.process_ref,
        window_generation=scene.window_ref.window_generation,
        correlated_at=time.time(),
    )
    with _LAUNCH_TARGETS_LOCK:
        _LAUNCH_TARGETS[application] = correlation


def _current_launch_target(application: str) -> _LaunchTargetCorrelation | None:
    identity = _application_identity(application)
    if not identity:
        return None
    now = time.time()
    with _LAUNCH_TARGETS_LOCK:
        correlation = _LAUNCH_TARGETS.get(identity)
        if (
            correlation is not None
            and now - correlation.correlated_at > _LAUNCH_TARGET_TTL_SECONDS
        ):
            _LAUNCH_TARGETS.pop(identity, None)
            return None
        return correlation


def _collapse_exact_scene_duplicates(
    scenes: Iterable[DesktopScene],
) -> tuple[DesktopScene, ...]:
    """Collapse exact native records and one proven UWP host/content pair."""

    grouped: dict[tuple[str, str], list[DesktopScene]] = {}
    for scene in scenes:
        resolution = DESKTOP_SURFACE_REGISTRY.resolve(scene.window_ref.ref_id)
        if resolution.status == "stale":
            continue
        key = (
            str(scene.window_ref.process_ref or ""),
            str(scene.window_ref.window_generation or ""),
        )
        if not all(key):
            key = ("scene", scene.window_ref.ref_id)
        grouped.setdefault(key, []).append(scene)
    collapsed = []
    for records in grouped.values():
        collapsed.append(max(
            records,
            key=lambda scene: (
                bool(scene.content_application_id),
                bool(scene.uia_tree_observed),
                len(scene.surfaces),
                float(scene.observed_at),
            ),
        ))
    logical = []
    for scene in collapsed:
        duplicate_index = next((
            index for index, existing in enumerate(logical)
            if _same_packaged_host_content_window(existing, scene)
        ), None)
        if duplicate_index is None:
            logical.append(scene)
            continue
        logical[duplicate_index] = max(
            (logical[duplicate_index], scene),
            key=lambda candidate: (
                bool(candidate.content_application_id),
                bool(candidate.packaged_application_id),
                bool(candidate.uia_tree_observed),
                len(candidate.surfaces),
                float(candidate.observed_at),
            ),
        )
    return tuple(logical)


def _same_packaged_host_content_window(
    first: DesktopScene,
    second: DesktopScene,
) -> bool:
    """Correlate one UWP frame and its inset content HWND, never peers."""

    for host, content in ((first, second), (second, first)):
        if (
            _application_identity(
                host.host_application_id or host.application_id
            ) != "applicationframehost"
            or not host.packaged_application_id
            or not host.content_application_id
            or _application_identity(host.content_application_id)
            != _application_identity(content.application_id)
            or _application_identity(
                content.host_application_id or content.application_id
            ) == "applicationframehost"
            or _normalized_surface_text(
                host.window_ref.accessible_label
            ) != _normalized_surface_text(
                content.window_ref.accessible_label
            )
        ):
            continue
        outer = host.window_ref.bounds
        inner = content.window_ref.bounds
        if outer is None or inner is None:
            continue
        insets = (
            inner.left - outer.left,
            inner.top - outer.top,
            outer.right - inner.right,
            outer.bottom - inner.bottom,
        )
        if all(0 <= value <= 32 for value in insets):
            return True
    return False


def _strategy_required_application() -> str:
    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None:
        return ""
    node = (
        strategy.node_by_id(strategy._frozen_node_id)
        if strategy.frozen_reason else None
    ) or strategy.goal
    return str(node.required_application or "").strip()


def _requested_application(target: str) -> str:
    required = _strategy_required_application()
    if required:
        return required
    value = " ".join(str(target or "").split())
    if not value or re.fullmatch(r"[\d\s\u00a0\u202f.,'*/+\-=×÷−]+", value):
        return ""
    return value[:160]


def _window_title_matches_application(title: str, expected: str) -> bool:
    target = _application_identity(expected)
    if not target:
        return False
    if _application_identity(title) == target:
        return True
    expected_words = _application_words(expected)
    title_words = _application_words(title)
    if not expected_words or len(expected_words) > len(title_words):
        return False
    width = len(expected_words)
    return any(
        title_words[index:index + width] == expected_words
        for index in range(len(title_words) - width + 1)
    )


def _safe_top_level_surface(
    *,
    application: str,
    title: str,
    class_name: str,
    foreground: bool,
) -> dict:
    return {
        "application": str(application or "")[:160],
        "title": _safe_label(title),
        "window_class": _safe_label(class_name),
        "foreground": bool(foreground),
    }


def _resolve_target_scene(
    scenes: Iterable[DesktopScene],
    requested_application: str,
    *,
    foreground_scene: DesktopScene | None,
    top_level_surfaces: Iterable[dict] = (),
) -> TargetSurfaceSelection:
    ranked = []
    for scene in _collapse_exact_scene_duplicates(scenes):
        rank, evidence = _scene_application_identity(
            scene, requested_application,
        )
        if rank > 0:
            ranked.append((rank, evidence, scene))
    if not ranked:
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="target_missing",
            foreground_scene=foreground_scene,
            target_scene=None,
            top_level_surfaces=tuple(top_level_surfaces),
        )
    correlation = _current_launch_target(requested_application)
    if correlation is not None:
        correlated = [
            item for item in ranked
            if (
                item[2].window_ref.process_ref == correlation.process_ref
                and item[2].window_ref.window_generation
                == correlation.window_generation
            )
        ]
        if len(correlated) == 1:
            rank, evidence, scene = correlated[0]
            return TargetSurfaceSelection(
                requested_application=requested_application,
                status="exact_target",
                foreground_scene=foreground_scene,
                target_scene=scene,
                identity_strength=rank,
                identity_evidence="current_open_attempt:" + evidence,
                candidate_count=1,
                top_level_surfaces=tuple(top_level_surfaces),
            )
    best_rank = max(item[0] for item in ranked)
    best = [item for item in ranked if item[0] == best_rank]
    if len(best) != 1:
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="target_ambiguous",
            foreground_scene=foreground_scene,
            target_scene=None,
            identity_strength=best_rank,
            candidate_count=len(best),
            top_level_surfaces=tuple(top_level_surfaces),
        )
    rank, evidence, scene = best[0]
    return TargetSurfaceSelection(
        requested_application=requested_application,
        status="exact_target",
        foreground_scene=foreground_scene,
        target_scene=scene,
        identity_strength=rank,
        identity_evidence=evidence,
        candidate_count=1,
        top_level_surfaces=tuple(top_level_surfaces),
    )


def _select_desktop_target(target: str = "") -> TargetSurfaceSelection:
    foreground_scene = observe_foreground_scene(target=target)
    requested_application = _requested_application(target)
    if foreground_scene is None:
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="desktop_surface_unavailable",
            foreground_scene=None,
            target_scene=None,
        )
    foreground_summary = _safe_top_level_surface(
        application=foreground_scene.application_id,
        title=foreground_scene.window_ref.accessible_label,
        class_name=(
            DESKTOP_SURFACE_REGISTRY.native_identity(
                foreground_scene.window_ref.ref_id,
            ).class_name
            if DESKTOP_SURFACE_REGISTRY.native_identity(
                foreground_scene.window_ref.ref_id,
            ) is not None else ""
        ),
        foreground=True,
    )
    if not requested_application:
        return TargetSurfaceSelection(
            requested_application="",
            status="current_foreground",
            foreground_scene=foreground_scene,
            target_scene=foreground_scene,
            identity_strength=100,
            identity_evidence="current_foreground_without_application_target",
            candidate_count=1,
            top_level_surfaces=(foreground_summary,),
        )
    foreground_rank, foreground_evidence = _scene_application_identity(
        foreground_scene, requested_application,
    )
    foreground_is_packaged_frame = bool(
        _application_identity(
            foreground_scene.host_application_id
            or foreground_scene.application_id
        ) == "applicationframehost"
        or foreground_scene.packaged_application_id
    )
    if foreground_rank > 0 and (
        sys.platform != "win32" or not foreground_is_packaged_frame
    ):
        return TargetSurfaceSelection(
            requested_application=requested_application,
            status="exact_target",
            foreground_scene=foreground_scene,
            target_scene=foreground_scene,
            identity_strength=foreground_rank,
            identity_evidence=foreground_evidence,
            candidate_count=1,
            top_level_surfaces=(foreground_summary,),
        )
    if sys.platform != "win32":
        return _resolve_target_scene(
            (), requested_application,
            foreground_scene=foreground_scene,
            top_level_surfaces=(foreground_summary,),
        )

    from osia.tools import desktop_control as dc

    foreground_native = DESKTOP_SURFACE_REGISTRY.native_identity(
        foreground_scene.window_ref.ref_id,
    )
    foreground_hwnd = (
        foreground_native.native_window_id if foreground_native is not None else None
    )
    candidates = [foreground_scene]
    summaries = [foreground_summary]
    for hwnd, title, pid, class_name in dc._enum_app_windows_win32()[:80]:
        if foreground_hwnd is not None and int(hwnd) == int(foreground_hwnd):
            continue
        host_application, _started_at = _process_details(pid)
        summaries.append(_safe_top_level_surface(
            application=host_application,
            title=title,
            class_name=class_name,
            foreground=False,
        ))
        host_identity = _application_identity(host_application)
        if not (
            host_identity == _application_identity(requested_application)
            or host_identity == "applicationframehost"
            or _window_title_matches_application(title, requested_application)
        ):
            continue
        observed = _observe_window_scene(
            (hwnd, title, pid),
            target=target,
            foreground=False,
            class_name_hint=class_name,
        )
        if observed is not None:
            candidates.append(observed)
    selection = _resolve_target_scene(
        candidates,
        requested_application,
        foreground_scene=foreground_scene,
        top_level_surfaces=summaries[:80],
    )
    if selection.target_scene is not None:
        _LAST_SCENE.set(selection.target_scene)
    return selection


def focus_existing_application_window(application: str) -> tuple[str, str]:
    """Focus one uniquely resolved existing app window without launching another."""

    if sys.platform != "win32":
        return "unavailable", ""
    selection = _select_desktop_target(application)
    scene = selection.target_scene
    if scene is None:
        return selection.status, ""
    if not (
        selection.identity_strength >= 350
        and (
            selection.identity_evidence.startswith("uia_content_application:")
            or selection.identity_evidence.startswith("executable_application:")
            or selection.identity_evidence.startswith("packaged_application_id:")
            or selection.identity_evidence.startswith("current_open_attempt:")
        )
    ):
        return "weak_identity", ""
    native = DESKTOP_SURFACE_REGISTRY.native_identity(
        scene.window_ref.ref_id,
    )
    if native is None:
        return "stale", ""
    detail = scene.window_ref.accessible_label
    if scene.foreground:
        _remember_launch_target(scene)
        return "ready", detail
    from osia.tools import desktop_control as dc

    try:
        status, focused_detail = dc.focus_app_window((
            native.native_window_id,
            detail,
            native.pid,
        ))
    except Exception:
        return "outcome_unknown", ""
    if status == "ready":
        _remember_launch_target(scene)
    return status, str(focused_detail or detail)


def _numeric_tokens(value: object) -> set[str]:
    tokens = set()
    for match in re.findall(
        r"(?<!\d)\d(?:[\s\u00a0\u202f.,']*\d)*(?!\d)",
        str(value or ""),
    ):
        normalized = re.sub(r"\D", "", match)
        if normalized:
            tokens.add(normalized)
    return tokens


def _contains_exact_integer(value: object, expected: str) -> bool:
    """Match an integer, allowing only unambiguous locale grouping."""

    for match in re.findall(
        r"(?<!\d)\d(?:[\s\u00a0\u202f.,']*\d)*(?!\d)",
        str(value or ""),
    ):
        compact = re.sub(r"[\s\u00a0\u202f']", "", match)
        if compact == expected:
            return True
        if len(expected) < 5:
            continue
        if re.fullmatch(
            r"\d{1,3}(?P<separator>[.,])\d{3}"
            r"(?:(?P=separator)\d{3})*",
            compact,
        ) and re.sub(r"[.,]", "", compact) == expected:
            return True
    return False


def rank_surface_candidates(
    surfaces: Iterable[DesktopSurfaceRef], target: str,
) -> tuple[DesktopSurfaceRef, ...]:
    query_text = str(target or "").lower().strip()
    query = {
        token for token in re.findall(r"[\w]+", query_text)
        if len(token) > 1
    }
    query_numbers = _numeric_tokens(query_text)
    ranked = []
    for surface in surfaces:
        if surface.surface_kind is SurfaceKind.WINDOW:
            continue
        label_tokens = set(re.findall(r"[\w]+", surface.accessible_label.lower()))
        numeric_match = bool(
            query_numbers
            and query_numbers.intersection(
                _numeric_tokens(surface.accessible_label),
            )
        )
        if query:
            overlap = len(query & label_tokens)
            exact = surface.accessible_label.lower().strip() == query_text
            if not overlap and not exact and not numeric_match:
                continue
        else:
            overlap, exact = 0, False
        score = (
            int(exact) * 100
            + int(numeric_match) * 80
            + overlap * 10
            + int(surface.enabled) * 2
            + int(surface.focused)
            + int(surface.selected)
        )
        ranked.append((score, surface))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[1] for item in ranked)


def _exact_observed_postcondition(
    scene: DesktopScene,
    target: str,
) -> tuple[str, str]:
    """Return the active required postcondition only for exact UIA state."""

    target_text = str(target or "").strip()
    if not target_text:
        return "", ""
    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None:
        return "", ""
    frozen_attempt = (
        strategy.attempt_by_id(strategy._frozen_attempt_id)
        if strategy.frozen_reason else None
    )
    if (
        frozen_attempt is not None
        and frozen_attempt.attempted_effect == "application_open"
        and frozen_attempt.target_application
        and scene_matches_application(
            scene, frozen_attempt.target_application,
        )
    ):
        expected = _application_identity(
            frozen_attempt.target_application
        )
        requested = _application_identity(target_text)
        if expected and (requested == expected or expected in requested):
            return frozen_attempt.attempted_effect, "application_open"
    goal = (
        strategy.node_by_id(strategy._frozen_node_id)
        if strategy.frozen_reason else None
    ) or strategy.goal
    postcondition = str(goal.observable_success_postcondition or "")
    if (
        not postcondition
        or not scene_matches_application(scene, goal.required_application)
    ):
        return "", ""
    if goal.requested_action == "open":
        expected = _application_identity(goal.required_application)
        requested = _application_identity(target_text)
        if expected and (requested == expected or expected in requested):
            return postcondition, "application_open"
    target_integer = re.sub(r"[\s\u00a0\u202f']", "", target_text)
    target_numbers = (
        {target_integer} if re.fullmatch(r"\d+", target_integer) else set()
    )
    if target_numbers:
        if not _contains_exact_integer(postcondition, target_integer):
            return "", ""
    elif target_text.lower() not in postcondition.lower():
        return "", ""
    for surface in scene.surfaces:
        if (
            not surface.enabled
            or surface.role not in {
                "DocumentControl", "EditControl", "PaneControl", "TextControl",
            }
        ):
            continue
        if target_numbers:
            label = surface.accessible_label.strip()
            label_is_number = bool(re.fullmatch(
                r"\d(?:[\s\u00a0\u202f.,']*\d)*",
                label,
            ))
            label_names_result = bool(re.search(
                r"\b(display|result|affichage|resultat|résultat)\b",
                label.lower(),
            ))
            if (
                (label_is_number or label_names_result)
                and _contains_exact_integer(label, target_integer)
            ):
                return postcondition, "exact_postcondition"
        elif surface.accessible_label.strip().lower() == target_text.lower():
            return postcondition, "exact_postcondition"
    return "", ""


def _exact_absent_postcondition(
    scene: DesktopScene,
    target: str,
) -> tuple[str, tuple[str, ...]]:
    """Prove a calculation absent only from a complete, neutral UIA state."""

    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None or not strategy.frozen_reason:
        return "", ()
    goal = strategy.node_by_id(strategy._frozen_node_id)
    if (
        goal is None
        or goal.requested_action != "calculate"
        or not scene.accessibility_complete
        or not scene.uia_tree_observed
        or not scene_matches_application(scene, goal.required_application)
    ):
        return "", ()
    postcondition = str(goal.observable_success_postcondition or "")
    target_integer = re.sub(
        r"[\s\u00a0\u202f']", "", str(target or "").strip(),
    )
    if (
        not postcondition
        or not re.fullmatch(r"\d+", target_integer)
        or not _contains_exact_integer(postcondition, target_integer)
    ):
        return "", ()
    state_labels = []
    for surface in scene.surfaces:
        if (
            not surface.enabled
            or surface.role not in {
                "DocumentControl", "EditControl", "PaneControl", "TextControl",
            }
        ):
            continue
        label = surface.accessible_label.strip()
        if not label:
            continue
        numeric_only = bool(re.fullmatch(
            r"\d(?:[\s\u00a0\u202f.,']*\d)*", label,
        ))
        names_state = bool(re.search(
            r"\b(display|result|output|input|equation|expression|"
            r"affichage|resultat|résultat)\b",
            label.lower(),
        ))
        if numeric_only or names_state:
            state_labels.append(label)
    if not state_labels:
        return "", ()
    observed_numbers = set().union(*(
        _numeric_tokens(label) for label in state_labels
    ))
    if target_integer in observed_numbers:
        return "", ()
    # A neutral zero-only state is the one generic calculation state that proves
    # both the requested result and every non-zero partial operand are absent.
    if not observed_numbers or any(number.strip("0") for number in observed_numbers):
        return "", ()
    if any(
        marker in label
        for label in state_labels
        for marker in ("+", "-", "−", "*", "×", "/", "÷", "=")
    ):
        return "", ()
    return postcondition, tuple(state_labels[:8])


def _primitive_state_projection(
    scene: DesktopScene,
    target: DesktopSurfaceRef | None = None,
    *,
    witness_surface_refs: Iterable[str] = (),
) -> dict:
    """Return bounded exact UIA state suitable for one primitive receipt."""

    state_rows = []
    for surface in scene.surfaces:
        if surface.role not in _PRIMITIVE_STATE_ROLES:
            continue
        label = surface.accessible_label.strip()
        if not label:
            continue
        identity = surface.runtime_signature or surface.ref_id
        state_rows.append({
            "surface_ref": surface.ref_id,
            "identity": identity,
            "role": surface.role,
            "label": label,
            "selected": bool(surface.selected),
            "focused": bool(surface.focused),
        })
    state_rows.sort(key=lambda item: (item["identity"], item["role"]))
    witness_refs = {
        str(item) for item in witness_surface_refs if str(item or "")
    }
    if witness_refs:
        witness_rows = [
            item for item in state_rows
            if item["surface_ref"] in witness_refs
        ][:_MAX_PRIMITIVE_STATE_ROWS]
        other_rows = [
            item for item in state_rows
            if item["surface_ref"] not in witness_refs
        ]
        state_rows = (
            witness_rows
            + other_rows[:max(
                0, _MAX_PRIMITIVE_STATE_ROWS - len(witness_rows),
            )]
        )
        state_rows.sort(key=lambda item: (item["identity"], item["role"]))
    else:
        state_rows = state_rows[:_MAX_PRIMITIVE_STATE_ROWS]
    encoded = json.dumps(
        state_rows,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload = {
        "application": scene.application_id,
        "window_surface_ref": scene.window_ref.ref_id,
        "window_generation": scene.window_ref.window_generation,
        "target_foreground": bool(scene.foreground),
        "structural_epoch": scene.structural_epoch,
        "observed_at": scene.observed_at,
        "state_signature": hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24],
        "state_rows": state_rows,
    }
    if target is not None:
        payload["target"] = target.to_safe_dict()
    return payload


def _normalized_surface_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _surface_text_matches(observed: object, expected: object) -> bool:
    """Match one declared exact accessible-state value, never a fuzzy label."""

    expected_text = " ".join(str(expected or "").split())
    observed_text = " ".join(str(observed or "").split())
    if not expected_text or not observed_text:
        return False
    expected_integer = re.sub(r"[\s\u00a0\u202f']", "", expected_text)
    if re.fullmatch(r"\d+", expected_integer):
        return _contains_exact_integer(observed_text, expected_integer)
    return _normalized_surface_text(observed_text) == _normalized_surface_text(
        expected_text,
    )


def _stale_primitive_continuity(
    scene: DesktopScene,
    attempt,
    before: dict,
    selection: TargetSurfaceSelection | None,
) -> tuple[DesktopSurfaceRef | None, dict]:
    """Rebind one stale UIA target only through unique structured identity."""

    primitive = (
        attempt.primitive_receipt
        if isinstance(attempt.primitive_receipt, dict) else {}
    )
    if primitive.get("kind") != "desktop_uia_primitive":
        return None, {}
    identity = (
        attempt.target_surface_identity
        if isinstance(attempt.target_surface_identity, dict) else {}
    )
    target_before = before.get("target")
    target_before = target_before if isinstance(target_before, dict) else {}
    signature = str(
        identity.get("runtime_signature")
        or target_before.get("runtime_signature")
        or ""
    )
    role = str(identity.get("role") or target_before.get("role") or "")
    expected_kind = str(identity.get("surface_kind") or "")
    expected_patterns = {
        str(item) for item in (
            identity.get("patterns") or target_before.get("patterns") or []
        ) if str(item)
    }
    prior_surface = str(
        target_before.get("surface_ref") or identity.get("surface_ref") or ""
    )
    prior_window = str(before.get("window_surface_ref") or "")
    prior_generation = str(before.get("window_generation") or "")
    raw_prior_epoch = before.get("structural_epoch")
    prior_epoch_valid = bool(
        isinstance(raw_prior_epoch, int)
        and not isinstance(raw_prior_epoch, bool)
        and raw_prior_epoch >= 0
    )
    prior_epoch = int(raw_prior_epoch) if prior_epoch_valid else 0
    same_generation_epoch_advanced = bool(
        prior_epoch_valid
        and scene.window_ref.window_generation == prior_generation
        and scene.structural_epoch > prior_epoch
    )
    strong_evidence = bool(
        selection is not None
        and selection.status == "exact_target"
        and selection.target_scene is scene
        and selection.candidate_count == 1
        and selection.identity_strength >= 350
        and str(selection.identity_evidence or "").startswith((
            "uia_content_application:",
            "executable_application:",
            "packaged_application_id:",
            "current_open_attempt:",
        ))
    )
    if (
        not strong_evidence
        or not signature
        or not role
        or not prior_surface
        or attempt.target_surface != prior_surface
        or str(identity.get("window_surface_ref") or prior_window) != prior_window
        or str(identity.get("window_generation") or prior_generation)
        != prior_generation
        or (
            scene.window_ref.window_generation == prior_generation
            and not same_generation_epoch_advanced
        )
        or scene.window_ref.ref_id == prior_window
        or (
            identity.get("process_ref")
            and scene.window_ref.process_ref
            != str(identity.get("process_ref") or "")
        )
    ):
        return None, {}
    candidates = [
        surface for surface in scene.surfaces
        if surface.enabled
        and surface.application_id == scene.application_id
        and surface.process_ref == scene.window_ref.process_ref
        and surface.window_generation == scene.window_ref.window_generation
        and surface.runtime_signature == signature
        and surface.role == role
        and (not expected_kind or surface.surface_kind.value == expected_kind)
        and (
            not expected_patterns
            or expected_patterns.issubset(set(surface.patterns))
        )
    ]
    if len(candidates) != 1:
        return None, {}
    target = candidates[0]
    return target, {
        "surface_continuity": "semantic_reresolution_proven",
        "previous_window_surface_ref": prior_window,
        "previous_window_generation": prior_generation,
        "rebound_window_surface_ref": scene.window_ref.ref_id,
        "rebound_window_generation": scene.window_ref.window_generation,
        "rebound_target_surface_ref": target.ref_id,
        "previous_structural_epoch": prior_epoch,
        "rebound_structural_epoch": scene.structural_epoch,
    }


def _exact_primitive_reconciliation(
    scene: DesktopScene,
    *,
    selection: TargetSurfaceSelection | None = None,
) -> dict:
    """Prove one UIA substep only from a fresh attributable state delta."""

    try:
        from osia.execution_strategy import current_execution_strategy

        strategy = current_execution_strategy()
    except Exception:
        strategy = None
    if strategy is None or not strategy.frozen_reason:
        return {}
    attempt = strategy.attempt_by_id(strategy._frozen_attempt_id)
    primitive = (
        attempt.primitive_receipt
        if attempt is not None and isinstance(attempt.primitive_receipt, dict)
        else {}
    )
    if (
        attempt is None
        or primitive.get("kind") not in {
            "desktop_uia_primitive", "desktop_window_focus",
        }
        or (
            primitive.get("kind") == "desktop_uia_primitive"
            and not attempt.substep_id
        )
    ):
        return {}
    before = primitive.get("before")
    before = before if isinstance(before, dict) else {}
    dispatched_at = max(
        float(attempt.dispatched_at or 0.0),
        float(primitive.get("dispatched_at") or 0.0),
    )
    if dispatched_at <= 0.0 or scene.observed_at < dispatched_at:
        return {}
    if _application_identity(scene.application_id) != _application_identity(
        before.get("application"),
    ):
        return {}

    primitive_kind = str(primitive.get("kind") or "")
    target_before = before.get("target")
    target_before = target_before if isinstance(target_before, dict) else {}
    target_signature = str(target_before.get("runtime_signature") or "")
    target_surface_ref = str(target_before.get("surface_ref") or "")
    target_now = next(
        (
            surface for surface in scene.surfaces
            if target_signature
            and surface.runtime_signature == target_signature
            and (
                not target_surface_ref
                or surface.ref_id == target_surface_ref
            )
        ),
        None,
    )
    expected_window = str(before.get("window_surface_ref") or "")
    expected_generation = str(before.get("window_generation") or "")
    continuity = {}
    if (
        scene.window_ref.ref_id == expected_window
        and scene.window_ref.window_generation == expected_generation
    ):
        expected_target = str(
            target_before.get("surface_ref") or attempt.target_surface or ""
        )
        if (
            primitive_kind == "desktop_uia_primitive"
            and (
                target_now is None
                or not expected_target
                or target_now.ref_id != expected_target
            )
        ):
            return {}
        continuity = {
            "surface_continuity": "exact_same_generation",
            "previous_window_surface_ref": expected_window,
            "previous_window_generation": expected_generation,
            "rebound_window_surface_ref": scene.window_ref.ref_id,
            "rebound_window_generation": scene.window_ref.window_generation,
            "rebound_target_surface_ref": (
                target_now.ref_id if target_now is not None else ""
            ),
        }
    else:
        target_now, continuity = _stale_primitive_continuity(
            scene, attempt, before, selection,
        )
        if target_now is None:
            return {}
    try:
        from osia.execution_strategy import normalize_desktop_observable_effect

        declared_effect = normalize_desktop_observable_effect(
            attempt.observable_effect,
        )
        receipt_effect = normalize_desktop_observable_effect(
            primitive.get("observable_effect"),
        )
    except Exception:
        declared_effect = {}
        receipt_effect = {}
    after = _primitive_state_projection(
        scene,
        target_now,
        witness_surface_refs=(declared_effect.get("surface_ref") or "",),
    )
    before_rows = {
        str(item.get("identity") or ""): item
        for item in before.get("state_rows") or []
        if isinstance(item, dict) and item.get("identity")
    }
    after_rows = {
        str(item.get("identity") or ""): item
        for item in after.get("state_rows") or []
        if isinstance(item, dict) and item.get("identity")
    }
    common = set(before_rows).intersection(after_rows)
    changed_state = [
        identity for identity in sorted(common)
        if any(
            before_rows[identity].get(field) != after_rows[identity].get(field)
            for field in ("label", "selected")
        )
    ]
    mechanism = str(primitive.get("mechanism") or "")
    effect_identity = next((
        str(item.get("identity") or "")
        for item in before_rows.values()
        if item.get("surface_ref") == declared_effect.get("surface_ref")
    ), "")
    before_effect = before_rows.get(effect_identity) if effect_identity else None
    after_effect = after_rows.get(effect_identity) if effect_identity else None
    exact_declared_effect = bool(
        mechanism in {
            "invoke", "selectionitem", "toggle", "expandcollapse",
        }
        and declared_effect
        and receipt_effect == declared_effect
        and isinstance(before_effect, dict)
        and isinstance(after_effect, dict)
        and _normalized_surface_text(before_effect.get("label"))
        != _normalized_surface_text(after_effect.get("label"))
        and not _surface_text_matches(
            before_effect.get("label"), declared_effect.get("expected_text"),
        )
        and _surface_text_matches(
            after_effect.get("label"), declared_effect.get("expected_text"),
        )
    )
    target_state_changed = bool(
        target_now is not None
        and target_before.get("selected") != target_now.selected
    )
    selection_mechanism = mechanism in {
        "selectionitem", "toggle", "expandcollapse",
    }
    focus_completed = bool(
        primitive_kind == "desktop_window_focus"
        and before.get("target_foreground") is False
        and scene.foreground
        and scene.window_ref.focused
    )
    exact_local_effect = bool(
        focus_completed
        or exact_declared_effect
        or (
            selection_mechanism
            and not declared_effect
            and target_state_changed
        )
    )
    if not exact_local_effect:
        before_signature = str(before.get("state_signature") or "")
        target_unchanged = bool(
            target_now is not None
            and str(target_before.get("label") or "")
            == target_now.accessible_label
            and target_before.get("selected") is target_now.selected
            and target_before.get("focused") is target_now.focused
            and target_before.get("enabled") is target_now.enabled
            and tuple(target_before.get("patterns") or ())
            == tuple(target_now.patterns)
        )
        exact_no_partial_effect = bool(
            primitive_kind == "desktop_uia_primitive"
            and mechanism in {
                "invoke", "selectionitem", "toggle", "expandcollapse",
            }
            and declared_effect
            and receipt_effect == declared_effect
            and primitive.get("worker_may_be_active") is not True
            and continuity.get("surface_continuity")
            == "exact_same_generation"
            and scene.observed_at - dispatched_at
            >= _PRIMITIVE_ABSENCE_SETTLE_SECONDS
            and before.get("target_foreground") is scene.foreground
            and before.get("structural_epoch") == scene.structural_epoch
            and before_signature
            and after.get("state_signature") == before_signature
            and not changed_state
            and target_unchanged
        )
        if exact_no_partial_effect:
            return {
                "schema_version": 1,
                "evidence_kind": "exact_independent_desktop_state",
                "evidence_source": "inspect_desktop_surfaces",
                "dispatch_state": "effect_absent",
                "outcome": "confirmed_absence",
                "reason_code": (
                    "exact_desktop_primitive_absent_no_partial_effect"
                ),
                "semantic_postcondition": "desktop_uia_primitive_absent",
                "postcondition": attempt.primitive_postcondition,
                "composite_id": attempt.composite_id,
                "substep_id": attempt.substep_id,
                "substep_index": attempt.substep_index,
                "strategy_attempt_id": attempt.attempt_id,
                "semantic_node_id": attempt.node_id,
                "mechanism_id": attempt.mechanism_id,
                "attempted_effect": attempt.attempted_effect,
                "primitive_postcondition": attempt.primitive_postcondition,
                "observable_effect": dict(declared_effect),
                "observed_application": scene.application_id,
                "observed_window_surface_ref": scene.window_ref.ref_id,
                "window_generation": scene.window_ref.window_generation,
                "structural_epoch": scene.structural_epoch,
                "observed_at": scene.observed_at,
                "changed_state_count": 0,
                "target_state_changed": False,
                "target_foreground": bool(scene.foreground),
                "no_partial_effect": True,
                **continuity,
            }
        return {}
    return {
        "schema_version": 1,
        "evidence_kind": "exact_independent_desktop_state",
        "evidence_source": "inspect_desktop_surfaces",
        "dispatch_state": "effect_confirmed",
        "outcome": "confirmed_success",
        "reason_code": (
            "exact_desktop_target_focus_observed"
            if focus_completed else
            "exact_desktop_observable_effect_change"
            if exact_declared_effect else
            "exact_desktop_primitive_state_change"
        ),
        "semantic_postcondition": (
            "application_foreground"
            if focus_completed else
            declared_effect.get("kind")
            if exact_declared_effect else
            "desktop_uia_primitive_completed"
        ),
        "postcondition": (
            attempt.attempted_effect
            if focus_completed else
            attempt.primitive_postcondition
        ),
        "composite_id": attempt.composite_id,
        "substep_id": attempt.substep_id,
        "substep_index": attempt.substep_index,
        "strategy_attempt_id": attempt.attempt_id,
        "semantic_node_id": attempt.node_id,
        "mechanism_id": attempt.mechanism_id,
        "attempted_effect": attempt.attempted_effect,
        "primitive_postcondition": attempt.primitive_postcondition,
        "observable_effect": dict(declared_effect),
        "observed_application": scene.application_id,
        "observed_window_surface_ref": scene.window_ref.ref_id,
        "window_generation": scene.window_ref.window_generation,
        "structural_epoch": scene.structural_epoch,
        "observed_at": scene.observed_at,
        "changed_state_count": len(changed_state),
        "target_state_changed": bool(target_state_changed or focus_completed),
        "target_foreground": bool(scene.foreground),
        **continuity,
    }


def inspect_desktop_surfaces(target: str = "") -> dict:
    selection = _select_desktop_target(target)
    scene = selection.target_scene
    foreground_scene = selection.foreground_scene
    if scene is None and selection.status == "desktop_surface_unavailable":
        return {
            "text": "ERROR: exact foreground desktop surface is unavailable.",
            "receipt": {"reason_code": "desktop_surface_unavailable"},
        }
    if scene is None:
        foreground_payload = (
            foreground_scene.safe_dict(target="")
            if foreground_scene is not None else {}
        )
        scene_payload = {
            "application": "",
            "requested_application": selection.requested_application,
            "target_resolution": selection.status,
            "target_foreground": False,
            "target_identity_strength": selection.identity_strength,
            "target_candidate_count": selection.candidate_count,
            "accessibility_quality": selection.status,
            "window_surface_ref": "",
            "window_generation": "",
            "structural_epoch": 0,
            "foreground_application": foreground_payload.get("application", ""),
            "foreground_window_surface_ref": foreground_payload.get(
                "window_surface_ref", "",
            ),
            "foreground_accessibility_quality": foreground_payload.get(
                "accessibility_quality", "",
            ),
            "top_level_surfaces": list(selection.top_level_surfaces),
            "surfaces": [],
        }
        reason_code = (
            "semantic_target_surface_ambiguous"
            if selection.status == "target_ambiguous" else
            "semantic_target_surface_not_found"
        )
        projection = {
            "instruction": (
                "The requested application has no unique exact desktop surface. "
                "Do not use foreground accessibility or Vision as target evidence."
            ),
            "scene": scene_payload,
        }
        return {
            "text": json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "scene": scene_payload,
            "receipt": {
                "outcome": "observed",
                "reason_code": reason_code,
                "requested_application": selection.requested_application,
                "target_resolution": selection.status,
                "target_candidate_count": selection.candidate_count,
                "target_foreground": False,
                "foreground_application": scene_payload[
                    "foreground_application"
                ],
            },
        }
    payload = scene.safe_dict(target=target)
    payload.update({
        "requested_application": selection.requested_application,
        "target_resolution": selection.status,
        "target_identity_strength": selection.identity_strength,
        "target_identity_evidence": selection.identity_evidence,
        "target_candidate_count": selection.candidate_count,
        "target_foreground": bool(scene.foreground),
        "foreground_application": (
            foreground_scene.application_id if foreground_scene is not None else ""
        ),
        "foreground_window_surface_ref": (
            foreground_scene.window_ref.ref_id
            if foreground_scene is not None else ""
        ),
        "foreground_accessibility_quality": (
            foreground_scene.accessibility_quality(target).value
            if foreground_scene is not None else ""
        ),
        "top_level_surfaces": list(selection.top_level_surfaces),
    })
    model_projection = {
        "instruction": (
            "Use the exact requested target scene, not an unrelated foreground. "
            "An enabled control whose actuation.background_safe is true may be "
            "invoked directly while its exact application remains in the "
            "background. Activate window_surface_ref only before a chosen "
            "capability whose preconditions require target_foreground; keyboard "
            "also requires fresh focus proof. Re-observe stale refs. Prefer native "
            "accessibility; visual evidence is observation."
        ),
        "scene": payload,
    }
    exact_postcondition, semantic_postcondition = (
        _exact_observed_postcondition(scene, target)
    )
    receipt = {
        "outcome": "observed",
        "reason_code": "semantic_desktop_surface_observed",
        "window_surface_ref": scene.window_ref.ref_id,
        "structural_epoch": scene.structural_epoch,
        "observed_at": scene.observed_at,
        "requested_application": selection.requested_application,
        "target_resolution": selection.status,
        "target_identity_evidence": selection.identity_evidence,
        "target_foreground": bool(scene.foreground),
        "foreground_application": payload["foreground_application"],
    }
    if exact_postcondition:
        receipt.update({
            "schema_version": 1,
            "evidence_kind": "exact_independent_desktop_state",
            "evidence_source": "inspect_desktop_surfaces",
            "dispatch_state": "effect_confirmed",
            "outcome": "confirmed_success",
            "reason_code": "exact_final_state_observation",
            "observed_target": str(target or "").strip(),
            "observed_application": scene.application_id,
            "observed_process_ref": scene.window_ref.process_ref,
            "observed_window_surface_ref": scene.window_ref.ref_id,
            "window_generation": scene.window_ref.window_generation,
            "semantic_postcondition": semantic_postcondition,
            "observed_state": semantic_postcondition,
            "postcondition": exact_postcondition,
        })
    else:
        absent_postcondition, neutral_state = _exact_absent_postcondition(
            scene, target,
        )
        if absent_postcondition:
            receipt.update({
                "schema_version": 1,
                "dispatch_state": "effect_absent",
                "outcome": "confirmed_absence",
                "reason_code": "exact_postcondition_absent_no_partial_effect",
                "observed_target": str(target or "").strip(),
                "postcondition": absent_postcondition,
                "no_partial_effect": True,
                "neutral_state": list(neutral_state),
            })
        else:
            primitive_reconciliation = _exact_primitive_reconciliation(
                scene,
                selection=selection,
            )
            if primitive_reconciliation:
                receipt.update(primitive_reconciliation)
    return {
        "text": json.dumps(
            model_projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "scene": payload,
        "receipt": receipt,
    }


def _capture_bounds(bounds: SurfaceBounds) -> bytes:
    return _capture_bounds_timed(bounds)[0]


def _capture_bounds_timed(bounds: SurfaceBounds) -> tuple[bytes, float, float]:
    if not bounds.valid():
        raise RuntimeError("invalid_surface_bounds")
    from PIL import ImageGrab

    _ensure_thread_physical_pixels()
    capture_started = time.perf_counter()
    image = ImageGrab.grab(
        bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom),
        all_screens=True,
    )
    capture_latency_ms = (time.perf_counter() - capture_started) * 1000.0
    preparation_started = time.perf_counter()
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    preparation_latency_ms = (time.perf_counter() - preparation_started) * 1000.0
    return output.getvalue(), capture_latency_ms, preparation_latency_ms


def request_visual_observation(
    surface_ref: str,
    reason: str,
    target: str = "",
    *,
    expected_application: str = "",
    expected_window_generation: str = "",
) -> dict:
    """Capture only a proven current surface for the exact effective model."""

    if not effective_model_supports_vision():
        return {
            "text": (
                "ERROR: effective model has no proven native Vision capability; "
                "no screenshot was captured or sent."
            ),
            "receipt": {"reason_code": "effective_model_non_vision"},
        }
    scene = observe_foreground_scene(target=target)
    if scene is None:
        return {
            "text": "ERROR: exact foreground desktop surface is unavailable.",
            "receipt": {"reason_code": "desktop_surface_unavailable"},
        }
    if (
        (expected_application and scene.application_id != expected_application)
        or (
            expected_window_generation
            and scene.window_ref.window_generation != expected_window_generation
        )
    ):
        return {
            "text": (
                "ERROR: exact desktop window changed before targeted visual "
                "observation; no screenshot was captured or sent."
            ),
            "receipt": {"reason_code": "desktop_surface_identity_changed"},
        }
    requested = next(
        (item for item in scene.surfaces if item.ref_id == str(surface_ref)),
        None,
    )
    if requested is None:
        resolved = DESKTOP_SURFACE_REGISTRY.resolve(str(surface_ref))
        return {
            "text": "ERROR: stale_reference; re-observe the desktop surface.",
            "receipt": {
                "reason_code": "stale_semantic_reference",
                "stale_reason": resolved.reason or "surface_not_in_current_window",
            },
        }
    if (
        scene.application_id in _SECURE_DESKTOP_PROCESS_NAMES
        or any(
            item.accessible_label == "[sensitive field]"
            for item in scene.surfaces
        )
    ):
        return {
            "text": (
                "ERROR: sensitive or secure desktop surface; no screenshot was "
                "captured or sent."
            ),
            "receipt": {"reason_code": "sensitive_capture_blocked"},
        }
    candidates = rank_surface_candidates(scene.surfaces, target)
    try:
        observation_reason = ObservationReason(str(reason))
    except ValueError:
        return {
            "text": "ERROR: unsupported visual observation reason.",
            "receipt": {"reason_code": "invalid_observation_reason"},
        }
    evidence = replace(
        scene.target_evidence(target, requested=requested),
        popup_suspected=bool(
            observation_reason is ObservationReason.POPUP_OR_OVERLAY
            and any(item.surface_kind is SurfaceKind.DIALOG for item in scene.surfaces)
        ),
        state_conflict=observation_reason is ObservationReason.STATE_CONFLICT,
        ambiguous_view=(
            observation_reason is ObservationReason.AMBIGUITY
            and len(candidates) > 1
        ),
        visual_postcondition=observation_reason is ObservationReason.VERIFICATION,
        structured_postcondition_complete=False,
    )
    store = _SCENE_STORES.get()
    if store is None:
        store = VisualObservationStore()
        _SCENE_STORES.set(store)
    current_observation = store.current(
        requested.ref_id,
        scene.structural_epoch,
    )
    decision = decide_visual_observation(
        evidence,
        model_supports_vision=True,
        budget_available=store.budget_allows(CaptureKind.WINDOW),
        existing_current_observation=current_observation is not None,
    )
    if not decision.eligible:
        if (
            decision.reason_code == "current_observation_reused"
            and current_observation is not None
        ):
            safe = current_observation.to_safe_dict()
            safe["deduplicated"] = True
            safe["new_image"] = False
            return {
                "text": json.dumps({
                    "instruction": (
                        "Reuse the prior image evidence only for this unchanged "
                        "surface epoch; keep deterministic actuation preferred."
                    ),
                    "observation": safe,
                    "scene": scene.safe_dict(target=target),
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "observation": safe,
                "scene": scene.safe_dict(target=target),
                "receipt": {
                    "outcome": "deduplicated",
                    "reason_code": decision.reason_code,
                    "surface_ref": requested.ref_id,
                    "window_surface_ref": scene.window_ref.ref_id,
                    "structural_epoch": scene.structural_epoch,
                    "image_sha256": current_observation.image_sha256,
                },
            }
        return {
            "text": "No visual observation was needed; structured evidence is sufficient.",
            "receipt": {"reason_code": decision.reason_code},
        }
    # Exact window is the privacy boundary. A control/pane crop stays inside it.
    bounds = requested.bounds or scene.window_ref.bounds
    if bounds is None:
        return {
            "text": "ERROR: scoped capture bounds are unavailable.",
            "receipt": {"reason_code": "scoped_capture_bounds_unavailable"},
        }
    window_bounds = scene.window_ref.bounds
    if window_bounds is not None and requested.ref_id != scene.window_ref.ref_id:
        bounds = SurfaceBounds(
            max(bounds.left, window_bounds.left),
            max(bounds.top, window_bounds.top),
            min(bounds.right, window_bounds.right),
            min(bounds.bottom, window_bounds.bottom),
        )
    try:
        png_bytes, capture_latency_ms, preparation_latency_ms = (
            _capture_bounds_timed(bounds)
        )
    except Exception:
        return {
            "text": "ERROR: scoped desktop capture failed; no global capture was used.",
            "receipt": {"reason_code": "scoped_capture_failed"},
        }
    capture_kind = decision.capture_kind or CaptureKind.WINDOW
    if requested.ref_id == scene.window_ref.ref_id:
        capture_kind = CaptureKind.WINDOW
    observation, is_new = store.add(
        surface_ref=requested.ref_id,
        capture_kind=capture_kind,
        reason=observation_reason,
        width=bounds.width,
        height=bounds.height,
        capture_epoch=store.capture_count,
        structural_epoch=scene.structural_epoch,
        surface_fingerprint=(
            f"{scene.application_id}:{scene.window_ref.window_generation}:"
            f"{scene.structural_epoch}"
        ),
        png_bytes=png_bytes,
    )
    safe = observation.to_safe_dict()
    safe["capture_latency_ms"] = round(capture_latency_ms, 3)
    safe["image_preparation_latency_ms"] = round(preparation_latency_ms, 3)
    safe["new_image"] = is_new
    result = {
        "text": json.dumps({
            "instruction": (
                "Use this targeted image only as semantic evidence. Choose an "
                "opaque surface ref, then keep UIA/keyboard ahead of coordinates."
            ),
            "observation": safe,
            "scene": scene.safe_dict(target=target),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "observation": safe,
        "scene": scene.safe_dict(target=target),
        "receipt": {
            "outcome": "observed" if is_new else "deduplicated",
            "reason_code": decision.reason_code,
            "surface_ref": requested.ref_id,
            "window_surface_ref": scene.window_ref.ref_id,
            "structural_epoch": scene.structural_epoch,
            "image_sha256": observation.image_sha256,
        },
    }
    if is_new:
        result["screenshot"] = store.image_base64(observation.observation_id)
    return result


def _find_runtime_control(root, runtime_id: tuple[int, ...]):
    frontier = [root]
    visited = 0
    while frontier and visited < _MAX_UIA_NODES:
        control = frontier.pop(0)
        visited += 1
        if _runtime_id(control) == runtime_id:
            return control
        try:
            frontier.extend(control.GetChildren() or ())
        except Exception:
            pass
    return None


def _pre_dispatch_failure(
    text: str,
    reason_code: str,
    *,
    failure_class: str = "mechanism_failed_continue",
    scene: DesktopScene | None = None,
    target: str = "",
) -> dict:
    """Build one canonical no-effect receipt and optional same-turn evidence."""

    receipt = {
        "schema_version": 1,
        "dispatch_state": "not_dispatched",
        "outcome": "confirmed_failure_before_effect",
        "reason_code": str(reason_code),
        "failure_class": str(failure_class),
    }
    result = {"text": str(text), "receipt": receipt}
    if scene is None:
        return result

    quality = scene.accessibility_quality(target)
    receipt["accessibility_quality"] = quality.value
    receipt["window_surface_ref"] = scene.window_ref.ref_id
    receipt["structural_epoch"] = scene.structural_epoch
    if (
        failure_class != "mechanism_failed_continue"
        or quality is AccessibilityQuality.RICH_TARGET_RESOLVED
    ):
        receipt["adaptive_visual_observation"] = "not_needed"
        return result
    if not effective_model_supports_vision():
        receipt["adaptive_visual_observation"] = "effective_model_non_vision"
        return result

    if any(
        item.surface_kind is SurfaceKind.DIALOG for item in scene.surfaces
    ):
        observation_reason = ObservationReason.POPUP_OR_OVERLAY
    elif quality is AccessibilityQuality.CUSTOM_RENDERED:
        observation_reason = ObservationReason.CUSTOM_RENDERED
    elif len(rank_surface_candidates(scene.surfaces, target)) > 1:
        observation_reason = ObservationReason.AMBIGUITY
    else:
        observation_reason = ObservationReason.INCOMPLETE_ACCESSIBILITY
    observation = request_visual_observation(
        scene.window_ref.ref_id,
        observation_reason.value,
        target,
        expected_application=scene.application_id,
        expected_window_generation=scene.window_ref.window_generation,
    )
    observation_receipt = (
        observation.get("receipt") if isinstance(observation, dict) else None
    )
    observation_receipt = (
        observation_receipt if isinstance(observation_receipt, dict) else {}
    )
    receipt["adaptive_visual_observation"] = str(
        observation_receipt.get("outcome")
        or observation_receipt.get("reason_code")
        or "unavailable"
    )
    receipt["visual_reason_code"] = str(
        observation_receipt.get("reason_code") or ""
    )
    for key in ("observation", "scene", "screenshot"):
        if key in observation:
            result[key] = observation[key]
    return result


def activate_desktop_surface(
    surface_ref: str,
    objective: str,
    *,
    observable_effect: dict | None = None,
    action_pattern: str = "",
    semantic_batch: list[dict] | None = None,
    _batch_internal: bool = False,
    _batch_application: str = "",
    _batch_window_generation: str = "",
    _batch_structural_epoch: int | None = None,
) -> dict:
    """Re-resolve an opaque ref and invoke/select it through UIA, never coordinates."""

    if sys.platform != "win32":
        return _pre_dispatch_failure(
            "ERROR: native semantic activation is unavailable on this platform.",
            "native_accessibility_unavailable",
        )

    if semantic_batch is not None and not _batch_internal:
        try:
            from osia.semantic_batch import (
                execute_semantic_batch,
                normalize_semantic_batch,
            )

            plan, reason_code = normalize_semantic_batch(
                surface_ref=surface_ref,
                objective=objective,
                action_pattern=action_pattern,
                observable_effect=observable_effect,
                semantic_batch=semantic_batch,
            )
        except Exception:
            plan, reason_code = {}, "semantic_batch_preflight_unavailable"
        if not plan:
            return _pre_dispatch_failure(
                "ERROR: semantic batch rejected before dispatch.",
                reason_code,
            )
        anchor_resolution = DESKTOP_SURFACE_REGISTRY.resolve(surface_ref)
        anchor = anchor_resolution.surface
        if not anchor_resolution.resolved or anchor is None:
            return _pre_dispatch_failure(
                "ERROR: stale semantic batch anchor.",
                "semantic_batch_stale_primitive",
            )

        def _dispatch_batch_item(item: dict) -> dict:
            item_result = activate_desktop_surface(
                str(item.get("surface_ref") or ""),
                str(item.get("objective") or ""),
                observable_effect=item.get("observable_effect"),
                action_pattern=str(item.get("action_pattern") or ""),
                _batch_internal=True,
                _batch_application=anchor.application_id,
                _batch_window_generation=anchor.window_generation,
                _batch_structural_epoch=anchor.structural_epoch,
            )
            try:
                from osia.tool_attempts import (
                    current_tool_attempt_id,
                    current_tool_attempt_store,
                )

                attempt_id = current_tool_attempt_id()
                store = current_tool_attempt_store()
                item_receipt = (
                    item_result.get("receipt")
                    if isinstance(item_result, dict) else None
                )
                if attempt_id and store is not None and isinstance(
                    item_receipt, dict
                ):
                    store.record_semantic_batch_primitive_receipt(
                        attempt_id,
                        semantic_batch_id=str(
                            plan.get("semantic_batch_id") or ""
                        ),
                        batch_index=int(item.get("index") or 0),
                        receipt=item_receipt,
                    )
            except Exception:
                # Dispatch lifecycle evidence remains fail-closed in the
                # canonical journal; the aggregate binder gets one final
                # chance to attach this exact typed receipt.
                pass
            return item_result

        return execute_semantic_batch(plan, _dispatch_batch_item)

    prior_resolution = DESKTOP_SURFACE_REGISTRY.resolve(surface_ref)
    prior_surface = prior_resolution.surface
    if prior_surface is None:
        return _pre_dispatch_failure(
            "ERROR: stale_reference; re-observe and re-resolve the semantic target.",
            "stale_semantic_reference",
        )
    selection = _select_desktop_target(prior_surface.application_id)
    scene = selection.target_scene
    if scene is None:
        return _pre_dispatch_failure(
            "ERROR: exact requested desktop surface is unavailable or ambiguous.",
            "semantic_target_surface_unavailable",
            failure_class="surface_unavailable",
        )
    if _batch_internal and (
        (_batch_application and scene.application_id != _batch_application)
        or (
            _batch_window_generation
            and scene.window_ref.window_generation
            != _batch_window_generation
        )
        or (
            _batch_structural_epoch is not None
            and scene.structural_epoch != int(_batch_structural_epoch)
        )
    ):
        return _pre_dispatch_failure(
            "ERROR: semantic batch target structure changed before dispatch.",
            "semantic_batch_structural_transition",
            scene=scene,
            target=str(objective or ""),
        )
    runtime_ids = {
        DESKTOP_SURFACE_REGISTRY.native_identity(item.ref_id).runtime_id
        for item in scene.surfaces
        if DESKTOP_SURFACE_REGISTRY.native_identity(item.ref_id) is not None
    }
    current = next(
        (item for item in scene.surfaces if item.ref_id == str(surface_ref)), None,
    )
    resolution = (
        prior_resolution
        if prior_resolution.status == "stale" else
        DESKTOP_SURFACE_REGISTRY.resolve(
            surface_ref,
            current_generation=scene.window_ref.window_generation,
            current_epoch=scene.structural_epoch,
            current_runtime_ids=runtime_ids,
            current_bounds=current.bounds if current is not None else None,
        )
    )
    reresolved_from = ""
    if not resolution.resolved or resolution.native is None:
        replacement = DESKTOP_SURFACE_REGISTRY.prove_equivalent(
            surface_ref,
            scene.surfaces,
            objective_label=str(objective or ""),
        )
        replacement_resolution = (
            DESKTOP_SURFACE_REGISTRY.resolve(
                replacement.ref_id,
                current_generation=scene.window_ref.window_generation,
                current_epoch=scene.structural_epoch,
                current_runtime_ids=runtime_ids,
                current_bounds=replacement.bounds,
            )
            if replacement is not None else None
        )
        if (
            replacement_resolution is not None
            and replacement_resolution.resolved
            and replacement_resolution.native is not None
        ):
            reresolved_from = str(surface_ref)
            surface_ref = replacement.ref_id
            resolution = replacement_resolution
        else:
            failure = _pre_dispatch_failure(
                "ERROR: stale_reference; re-observe and re-resolve the semantic target.",
                "stale_semantic_reference",
                scene=scene,
                target=str(objective or ""),
            )
            failure["receipt"]["stale_reason"] = resolution.reason
            return failure
    normalized_objective = " ".join(str(objective or "").lower().split())
    normalized_label = " ".join(
        str(resolution.surface.accessible_label or "").lower().split()
    )
    if (
        not normalized_objective
        or not normalized_label
        or normalized_objective != normalized_label
    ):
        failure = _pre_dispatch_failure(
            (
                "ERROR: stale_reference; the stated objective no longer exactly "
                "matches the resolved semantic surface."
            ),
            "stale_semantic_reference",
            scene=scene,
            target=str(objective or ""),
        )
        failure["receipt"]["stale_reason"] = "objective_surface_mismatch"
        return failure
    is_window_target = bool(
        resolution.surface.ref_id == scene.window_ref.ref_id
        and resolution.surface.surface_kind in {
            SurfaceKind.WINDOW, SurfaceKind.DIALOG,
        }
    )
    selected_pattern = next((
        pattern_name
        for pattern_name, _getter_name, _method_name
        in _UIA_ACTION_PATTERN_METHODS
        if pattern_name.casefold() == str(action_pattern or "").casefold()
    ), "")
    window_pattern = (
        is_window_target
        and str(action_pattern or "").casefold() == "window"
    )
    if action_pattern and not selected_pattern and not window_pattern:
        return _pre_dispatch_failure(
            "ERROR: requested UIA action pattern is not supported by OSIA.",
            "uia_action_pattern_unsupported",
            scene=scene,
            target=str(objective or ""),
        )
    if is_window_target:
        if scene.foreground:
            return _pre_dispatch_failure(
                "ERROR: exact target window is already foreground; re-observe it.",
                "target_already_foreground",
                scene=scene,
                target=str(objective or ""),
            )
        from osia.tools import desktop_control as dc

        journal_started, journal_returned = (
            _durable_primitive_journal_callbacks(
                application_identity=scene.application_id,
                surface_ref=surface_ref,
                native_window_id=resolution.native.native_window_id,
                window_generation=scene.window_ref.window_generation,
                runtime_identity=resolution.surface.runtime_signature,
            )
        )
        if callable(journal_started) and not journal_started(
            uia_pattern="Window",
        ):
            return _pre_dispatch_failure(
                "ERROR: durable primitive journal unavailable before focus dispatch.",
                "primitive_journal_unavailable",
                scene=scene,
                target=str(objective or ""),
            )
        dispatched_at = time.time()
        try:
            focus_status, _focus_detail = dc.focus_app_window((
                resolution.native.native_window_id,
                resolution.surface.accessible_label,
                resolution.native.pid,
            ))
        except Exception:
            focus_status = "outcome_unknown"
        if callable(journal_returned) and not journal_returned():
            focus_status = "outcome_unknown"
        if focus_status != "ready":
            return {
                "text": "ERROR: exact target-window focus is unverified after dispatch.",
                "receipt": {
                    "schema_version": 1,
                    "dispatch_state": "effect_unknown",
                    "outcome": "outcome_unknown",
                    "reason_code": "desktop_target_focus_outcome_unknown",
                    "failure_class": "effect_reconciliation_required",
                    "surface_ref": surface_ref,
                    "primitive": {
                        "kind": "desktop_window_focus",
                        "mechanism": "exact_window_focus",
                        "dispatched_at": dispatched_at,
                        "before": _primitive_state_projection(
                            scene, resolution.surface,
                        ),
                    },
                },
            }
        return {
            "text": (
                "Exact desktop target-window activation dispatched; read-only "
                "re-observation is required before actuation."
            ),
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "dispatched",
                "outcome": "success_unverified",
                "reason_code": "desktop_target_focus_dispatched",
                "surface_ref": surface_ref,
                "mechanism": "exact_window_focus",
                "structural_epoch": scene.structural_epoch,
                "reresolved_from": reresolved_from,
                "primitive": {
                    "kind": "desktop_window_focus",
                    "mechanism": "exact_window_focus",
                    "dispatched_at": dispatched_at,
                    "before": _primitive_state_projection(
                        scene, resolution.surface,
                    ),
                },
            },
        }
    actuation_contract = _surface_actuation_contract(
        resolution.surface,
        target_foreground=scene.foreground,
        target_minimized=scene.minimized,
    )
    if scene.minimized:
        failure = _pre_dispatch_failure(
            (
                "ERROR: exact target is minimized; restore its window_surface_ref "
                "and re-observe before control actuation."
            ),
            "target_restored_required",
            failure_class="surface_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
        failure["receipt"]["target_window_surface_ref"] = scene.window_ref.ref_id
        return failure
    if not scene.foreground and not actuation_contract.get("background_safe"):
        failure = _pre_dispatch_failure(
            (
                "ERROR: exact target is background; activate its window_surface_ref "
                "and re-observe focus before UIA actuation."
            ),
            "target_focus_verification_required",
            failure_class="surface_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
        failure["receipt"]["target_window_surface_ref"] = scene.window_ref.ref_id
        return failure
    if not resolution.native.runtime_id:
        return _pre_dispatch_failure(
            "ERROR: surface has no actionable accessibility runtime identity.",
            "uia_runtime_id_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
    from osia.tools import desktop_control as dc

    auto = dc._load_uiautomation()
    if auto is None:
        return _pre_dispatch_failure(
            "ERROR: native accessibility is unavailable.",
            "native_accessibility_unavailable",
            scene=scene,
            target=str(objective or ""),
        )
    try:
        from osia.execution_strategy import normalize_desktop_observable_effect

        declared_effect = normalize_desktop_observable_effect(
            observable_effect,
        )
    except Exception:
        declared_effect = {}
    primitive_before = _primitive_state_projection(
        scene,
        resolution.surface,
        witness_surface_refs=(declared_effect.get("surface_ref") or "",),
    )
    dispatch_started = False
    dispatch_started_at = 0.0
    dispatch_worker_may_be_active = False
    attempted_mechanism = ""
    pre_dispatch_failure = None
    actionability = {
        "candidate_identity": {
            "surface_ref": resolution.surface.ref_id,
            "runtime_signature": resolution.surface.runtime_signature,
            "role": resolution.surface.role,
            "label": resolution.surface.accessible_label,
            "window_surface_ref": scene.window_ref.ref_id,
            "window_generation": scene.window_ref.window_generation,
            "structural_epoch": scene.structural_epoch,
        },
        "automation_id": "",
        "semantic_match": True,
        "observed_patterns": [
            item for item in resolution.surface.patterns
            if item in _BACKGROUND_SAFE_UIA_PATTERNS
        ],
        "fresh_supported_patterns": [],
        "selected_pattern": selected_pattern,
        "selected_pattern_available": False,
        "actionable_match": False,
        "candidate_applicable": False,
        "dispatch_preflight_result": "pending_fresh_reference_check",
        "failed_before_effect_reason": "",
        "stale_rebind_status": (
            "reresolved" if reresolved_from else "fresh_exact_reference"
        ),
        "retirement_state": "active",
    }
    invoke_preflight_reason = ""
    if declared_effect:
        effect_surface = next((
            item for item in scene.surfaces
            if item.ref_id == declared_effect["surface_ref"]
            and item.role in _PRIMITIVE_STATE_ROLES
        ), None)
        if (
            effect_surface is None
            or effect_surface.ref_id == resolution.surface.ref_id
            or _surface_text_matches(
                effect_surface.accessible_label,
                declared_effect["expected_text"],
            )
        ):
            invoke_preflight_reason = (
                "observable_effect_precondition_unverified"
            )
    try:
        journal_started, journal_returned = (
            _durable_primitive_journal_callbacks(
                application_identity=scene.application_id,
                surface_ref=surface_ref,
                native_window_id=resolution.native.native_window_id,
                window_generation=scene.window_ref.window_generation,
                runtime_identity=resolution.surface.runtime_signature,
            )
        )
        dispatch = _dispatch_fresh_uia_action_bounded(
            auto,
            native_window_id=resolution.native.native_window_id,
            runtime_id=resolution.native.runtime_id,
            action_pattern=selected_pattern,
            observed_patterns=resolution.surface.patterns,
            invoke_preflight_reason=invoke_preflight_reason,
            journal_dispatch_started=journal_started,
            journal_dispatch_returned=journal_returned,
        )
        selected_pattern = str(dispatch.get("selected_pattern") or "")
        actionability.update({
            "automation_id": str(dispatch.get("automation_id") or ""),
            "fresh_supported_patterns": list(
                dispatch.get("fresh_supported_patterns") or []
            ),
            "selected_pattern": selected_pattern,
            "selected_pattern_available": bool(
                dispatch.get("selected_pattern_available")
            ),
            "actionable_match": bool(
                dispatch.get("selected_pattern_available")
            ),
            "candidate_applicable": bool(
                dispatch.get("selected_pattern_available")
            ),
        })
        attempted_mechanism = selected_pattern.casefold()
        dispatch_started_at = float(dispatch.get("dispatched_at") or 0.0)
        dispatch_started = bool(dispatch.get("dispatch_started"))
        dispatch_worker_may_be_active = bool(
            dispatch.get("worker_may_be_active")
        )
        if not dispatch_started:
            reason_code = str(dispatch.get("reason_code") or (
                "native_accessibility_invocation_unavailable"
            ))
            if reason_code == "stale_semantic_reference":
                DESKTOP_SURFACE_REGISTRY.mark_stale(
                    surface_ref, "accessibility_runtime_id_disappeared",
                )
                failure_text = (
                    "ERROR: stale_reference; accessibility target disappeared."
                )
                stale_reason = "accessibility_runtime_id_disappeared"
                actionability["stale_rebind_status"] = (
                    "fresh_runtime_reference_missing"
                )
            elif reason_code == "observable_effect_precondition_unverified":
                failure_text = (
                    "ERROR: declared observable effect is not a fresh exact "
                    "surface transition."
                )
                stale_reason = ""
            elif reason_code == "uia_action_pattern_unavailable":
                failure_text = (
                    "ERROR: accessibility control has no safe actionable pattern."
                )
                stale_reason = ""
            else:
                failure_text = (
                    "ERROR: native accessibility invocation was unavailable "
                    "before dispatch."
                )
                stale_reason = ""
            pre_dispatch_failure = (
                failure_text,
                reason_code,
                stale_reason,
            )
            actionability.update({
                "dispatch_preflight_result": "failed_before_effect",
                "failed_before_effect_reason": reason_code,
                "retirement_state": "eligible_for_exact_pair_retirement",
            })
            mechanism = ""
        elif not dispatch.get("completed"):
            raise RuntimeError(
                str(dispatch.get("reason_code") or (
                    "uia_activation_outcome_unknown"
                ))
            )
        else:
            mechanism = attempted_mechanism
    except Exception:
        if not dispatch_started:
            return _pre_dispatch_failure(
                "ERROR: native accessibility invocation was unavailable before dispatch.",
                "native_accessibility_invocation_unavailable",
                scene=scene,
                target=str(objective or ""),
            )
        return {
            "text": "ERROR: native semantic activation failed after possible effect.",
            "receipt": {
                "schema_version": 1,
                "dispatch_state": "effect_unknown",
                "outcome": "outcome_unknown",
                "reason_code": "uia_activation_outcome_unknown",
                "failure_class": "effect_reconciliation_required",
                "surface_ref": surface_ref,
                "structural_epoch": scene.structural_epoch,
                "reresolved_from": reresolved_from,
                "actionability": {
                    **actionability,
                    "dispatch_preflight_result": "effect_unknown",
                    "retirement_state": "reconciliation_required",
                },
                "primitive": {
                    "kind": "desktop_uia_primitive",
                    "mechanism": attempted_mechanism,
                    "observable_effect": dict(declared_effect),
                    "dispatched_at": dispatch_started_at,
                    "worker_may_be_active": (
                        dispatch_worker_may_be_active
                    ),
                    "before": primitive_before,
                },
            },
        }
    if pre_dispatch_failure is not None:
        text, reason_code, stale_reason = pre_dispatch_failure
        failure = _pre_dispatch_failure(
            text,
            reason_code,
            scene=scene,
            target=str(objective or ""),
        )
        if stale_reason:
            failure["receipt"]["stale_reason"] = stale_reason
        failure["receipt"].update({
            "surface_ref": surface_ref,
            "runtime_identity": resolution.surface.runtime_signature,
            "automation_id": str(actionability.get("automation_id") or ""),
            "uia_pattern": str(
                actionability.get("selected_pattern") or selected_pattern or ""
            ),
        })
        failure["receipt"]["actionability"] = actionability
        return failure
    actionability["dispatch_preflight_result"] = "dispatched"
    actionability["retirement_state"] = "active_pending_verification"
    return {
        "text": "Desktop surface activated through native accessibility.",
        "receipt": {
            "schema_version": 1,
            "dispatch_state": "dispatched",
            "outcome": "success_unverified",
            "reason_code": "native_accessibility_dispatched",
            "surface_ref": surface_ref,
            "mechanism": mechanism,
            "actionability": actionability,
            "actuation_preconditions": list(
                actuation_contract.get("preconditions") or []
            ),
            "background_safe": bool(
                actuation_contract.get("background_safe")
            ),
            "structural_epoch": scene.structural_epoch,
            "reresolved_from": reresolved_from,
            "primitive": {
                "kind": "desktop_uia_primitive",
                "mechanism": mechanism,
                "observable_effect": dict(declared_effect),
                "dispatched_at": dispatch_started_at,
                "worker_may_be_active": dispatch_worker_may_be_active,
                "before": primitive_before,
            },
        },
    }


__all__ = [
    "DesktopScene",
    "activate_desktop_surface",
    "begin_desktop_turn",
    "bind_effective_model",
    "effective_model_supports_vision",
    "effective_model_image_route",
    "end_desktop_turn",
    "focus_existing_application_window",
    "inspect_desktop_surfaces",
    "observe_foreground_scene",
    "rank_surface_candidates",
    "request_visual_observation",
    "reset_effective_model",
    "scene_matches_application",
]
