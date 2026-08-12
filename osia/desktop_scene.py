"""Generic semantic observation and bounded capture for native desktop apps."""

from __future__ import annotations

import contextvars
import hashlib
import io
import json
import re
import sys
import time
from dataclasses import dataclass
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
    DesktopEvidence,
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

    def safe_dict(self, *, target: str = "") -> dict:
        candidates = rank_surface_candidates(self.surfaces, target)
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
            "window_surface_ref": self.window_ref.ref_id,
            "window_generation": self.window_ref.window_generation,
            "structural_epoch": self.structural_epoch,
            "accessibility_complete": self.accessibility_complete,
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
                item.to_safe_dict() for item in projected
            ],
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


def _runtime_signature(runtime_id: tuple[int, ...], role: str, ancestor: str) -> str:
    if not runtime_id:
        return ""
    material = f"{runtime_id!r}\x1f{role}\x1f{ancestor}"
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
            if callable(getter) and getter() is not None:
                supported.append(safe_name)
        except Exception:
            continue
    return tuple(supported)


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


def observe_foreground_scene(*, target: str = "") -> DesktopScene | None:
    """Observe one exact foreground window without exposing its native handle."""

    if sys.platform != "win32":
        return None
    _ensure_thread_physical_pixels()
    from osia.tools import desktop_control as dc

    foreground = dc._foreground_window_win32()
    if not foreground:
        return None
    hwnd, title, pid = foreground
    if not hwnd or not pid:
        return None
    window_bounds = _window_bounds(hwnd)
    if window_bounds is None:
        return None
    application_id, process_started_at = _process_details(pid)
    class_name = ""
    try:
        loaded = dc._load_win32()
        class_name = loaded[0].GetClassName(hwnd) if loaded else ""
    except Exception:
        class_name = ""

    rows = []
    controls = []
    root_is_modal = False
    auto = dc._load_uiautomation()
    if auto is not None:
        try:
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(int(hwnd))
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

    signature = _scene_signature(
        rows or [(class_name, window_bounds.to_safe_dict(), root_is_modal)]
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
        selected=True,
        focused=True,
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
            runtime_id, role, item["ancestor"],
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
            focused=item["focused"],
            enabled=item["enabled"],
            parent_ref=parent_ref,
            bounds=bounds,
            runtime_signature=runtime_signature,
            structural_epoch=epoch,
            scene_signature=signature,
            native=native,
            stable_identity=stable_identity,
            patterns=item["patterns"],
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
    )
    _LAST_SCENE.set(scene)
    return scene


def rank_surface_candidates(
    surfaces: Iterable[DesktopSurfaceRef], target: str,
) -> tuple[DesktopSurfaceRef, ...]:
    query = {token for token in re.findall(r"[\w]+", str(target or "").lower()) if len(token) > 1}
    ranked = []
    for surface in surfaces:
        if surface.surface_kind is SurfaceKind.WINDOW:
            continue
        label_tokens = set(re.findall(r"[\w]+", surface.accessible_label.lower()))
        if query:
            overlap = len(query & label_tokens)
            exact = surface.accessible_label.lower().strip() == str(target).lower().strip()
            if not overlap and not exact:
                continue
        else:
            overlap, exact = 0, False
        score = (
            int(exact) * 100
            + overlap * 10
            + int(surface.enabled) * 2
            + int(surface.focused)
            + int(surface.selected)
        )
        ranked.append((score, surface))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return tuple(item[1] for item in ranked)


def inspect_desktop_surfaces(target: str = "") -> dict:
    scene = observe_foreground_scene(target=target)
    if scene is None:
        return {
            "text": "ERROR: exact foreground desktop surface is unavailable.",
            "receipt": {"reason_code": "desktop_surface_unavailable"},
        }
    payload = scene.safe_dict(target=target)
    model_projection = {
        "instruction": (
            "Use opaque refs as authority. Re-observe stale refs. Prefer native "
            "accessibility or keyboard for action; visual evidence is observation."
        ),
        "scene": payload,
    }
    return {
        "text": json.dumps(
            model_projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "scene": payload,
        "receipt": {
            "outcome": "observed",
            "reason_code": "semantic_desktop_surface_observed",
            "window_surface_ref": scene.window_ref.ref_id,
            "structural_epoch": scene.structural_epoch,
        },
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
    evidence = DesktopEvidence(
        candidate_count=len(candidates),
        actionable_candidate_count=sum(item.enabled for item in candidates),
        accessible_node_count=len(scene.surfaces) - 1,
        custom_rendered=not scene.accessibility_complete,
        popup_suspected=bool(
            observation_reason is ObservationReason.POPUP_OR_OVERLAY
            and any(item.surface_kind is SurfaceKind.DIALOG for item in scene.surfaces)
        ),
        small_target=bool(
            requested.bounds
            and (requested.bounds.width < 48 or requested.bounds.height < 28)
        ),
        state_conflict=observation_reason is ObservationReason.STATE_CONFLICT,
        ambiguous_view=(
            observation_reason is ObservationReason.AMBIGUITY
            and len(candidates) > 1
        ),
        visual_postcondition=observation_reason is ObservationReason.VERIFICATION,
        structured_postcondition_complete=False,
        local_region_available=requested.bounds is not None,
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


def activate_desktop_surface(surface_ref: str, objective: str) -> dict:
    """Re-resolve an opaque ref and invoke/select it through UIA, never coordinates."""

    if sys.platform != "win32":
        return {
            "text": "ERROR: native semantic activation is unavailable on this platform.",
            "receipt": {"reason_code": "native_accessibility_unavailable"},
        }
    scene = observe_foreground_scene()
    if scene is None:
        return {
            "text": "ERROR: exact foreground desktop surface is unavailable.",
            "receipt": {"reason_code": "desktop_surface_unavailable"},
        }
    runtime_ids = {
        DESKTOP_SURFACE_REGISTRY.native_identity(item.ref_id).runtime_id
        for item in scene.surfaces
        if DESKTOP_SURFACE_REGISTRY.native_identity(item.ref_id) is not None
    }
    current = next(
        (item for item in scene.surfaces if item.ref_id == str(surface_ref)), None,
    )
    resolution = DESKTOP_SURFACE_REGISTRY.resolve(
        surface_ref,
        current_generation=scene.window_ref.window_generation,
        current_epoch=scene.structural_epoch,
        current_runtime_ids=runtime_ids,
        current_bounds=current.bounds if current is not None else None,
    )
    if not resolution.resolved or resolution.native is None:
        return {
            "text": "ERROR: stale_reference; re-observe and re-resolve the semantic target.",
            "receipt": {
                "outcome": "not_dispatched",
                "reason_code": "stale_semantic_reference",
                "stale_reason": resolution.reason,
            },
        }
    normalized_objective = " ".join(str(objective or "").lower().split())
    normalized_label = " ".join(
        str(resolution.surface.accessible_label or "").lower().split()
    )
    if (
        not normalized_objective
        or not normalized_label
        or normalized_objective != normalized_label
    ):
        return {
            "text": (
                "ERROR: stale_reference; the stated objective no longer exactly "
                "matches the resolved semantic surface."
            ),
            "receipt": {
                "outcome": "not_dispatched",
                "reason_code": "stale_semantic_reference",
                "stale_reason": "objective_surface_mismatch",
            },
        }
    if not resolution.native.runtime_id:
        return {
            "text": "ERROR: surface has no actionable accessibility runtime identity.",
            "receipt": {"reason_code": "uia_runtime_id_unavailable"},
        }
    from osia.tools import desktop_control as dc

    auto = dc._load_uiautomation()
    if auto is None:
        return {
            "text": "ERROR: native accessibility is unavailable.",
            "receipt": {"reason_code": "native_accessibility_unavailable"},
        }
    try:
        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(int(resolution.native.native_window_id))
            control = _find_runtime_control(root, resolution.native.runtime_id)
            if control is None:
                DESKTOP_SURFACE_REGISTRY.mark_stale(
                    surface_ref, "accessibility_runtime_id_disappeared",
                )
                return {
                    "text": "ERROR: stale_reference; accessibility target disappeared.",
                    "receipt": {
                        "outcome": "not_dispatched",
                        "reason_code": "stale_semantic_reference",
                        "stale_reason": "accessibility_runtime_id_disappeared",
                    },
                }
            mechanism = ""
            for pattern_name, method_name in (
                ("GetInvokePattern", "Invoke"),
                ("GetSelectionItemPattern", "Select"),
                ("GetTogglePattern", "Toggle"),
                ("GetExpandCollapsePattern", "Expand"),
            ):
                try:
                    pattern = getattr(control, pattern_name)()
                    method = getattr(pattern, method_name, None) if pattern else None
                    if callable(method):
                        method()
                        mechanism = pattern_name[3:-7].lower()
                        break
                except Exception:
                    continue
            if not mechanism:
                return {
                    "text": "ERROR: accessibility control has no safe actionable pattern.",
                    "receipt": {"reason_code": "uia_action_pattern_unavailable"},
                }
    except Exception:
        return {
            "text": "ERROR: native semantic activation failed after possible effect.",
            "receipt": {
                "outcome": "outcome_unknown",
                "reason_code": "uia_activation_outcome_unknown",
            },
        }
    return {
        "text": "Desktop surface activated through native accessibility.",
        "receipt": {
            "outcome": "success_unverified",
            "reason_code": "native_accessibility_dispatched",
            "surface_ref": surface_ref,
            "mechanism": mechanism,
            "structural_epoch": scene.structural_epoch,
        },
    }


__all__ = [
    "DesktopScene",
    "activate_desktop_surface",
    "begin_desktop_turn",
    "bind_effective_model",
    "effective_model_supports_vision",
    "end_desktop_turn",
    "inspect_desktop_surfaces",
    "observe_foreground_scene",
    "rank_surface_candidates",
    "request_visual_observation",
    "reset_effective_model",
]
