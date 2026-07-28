"""Opaque browser identities, bounded waits and deterministic action receipts.

This module contains no Playwright dependency.  The async browser owner keeps
the actual objects private and uses this registry as the public trust boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


BROWSER_STATE_SCHEMA_VERSION = 1
ACTION_RECEIPT_SCHEMA_VERSION = 1
MAX_WAIT_SECONDS = 60.0
MAX_SAFE_EXPECTED_TEXT = 4_000
MAX_LIFECYCLE_EVENTS = 128

_OPAQUE_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}_[A-Za-z0-9_-]{16,64}$")


def opaque_ref(prefix: str) -> str:
    clean = re.sub(r"[^a-z0-9_]", "", str(prefix).lower())[:12] or "ref"
    return f"{clean}_{secrets.token_urlsafe(18)}"


def safe_url_display(value: Any) -> str:
    """Return a bounded query/fragment/userinfo-free display URL."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "invalid-url"
    scheme = parsed.scheme.lower()
    if scheme in {"about", "data", "blob"}:
        if scheme == "about" and parsed.path in {"blank", "newtab"}:
            return f"about:{parsed.path}"
        return f"{scheme}:private"
    if scheme == "file":
        return "file:///[private]"
    if scheme not in {"http", "https"}:
        return f"{scheme or 'unknown'}://[private]"
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        host = ""
    if not host:
        return "invalid-url"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parsed.path
    if len(path) > 512:
        path = path[:509] + "..."
    return urlunsplit((scheme, netloc, path, "", ""))


def safe_origin(value: Any) -> str:
    display = safe_url_display(value)
    try:
        parsed = urlsplit(display)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return parsed.scheme + ":" if parsed.scheme else ""
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def scrub_browser_error(error: Any) -> str:
    """Map private Playwright exceptions to stable value-free categories."""

    name = type(error).__name__.lower()
    text = str(error or "").lower()
    if "timeout" in name or "timeout" in text or "timed out" in text:
        return "browser_timeout"
    if "closed" in text or "target page" in text or "context" in text:
        return "browser_state_closed"
    if "not found" in text or "no node" in text:
        return "target_not_found"
    if "strict mode" in text or "multiple" in text:
        return "target_ambiguous"
    if "download" in text:
        return "download_failed"
    if "file chooser" in text:
        return "file_chooser_failed"
    return "browser_operation_failed"


class BrowserReferenceError(RuntimeError):
    def __init__(self, code: str = "stale_browser_reference"):
        super().__init__(code)
        self.code = code


class DispatchState(str, Enum):
    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED = "dispatched"
    EFFECT_CONFIRMED = "effect_confirmed"
    EFFECT_UNKNOWN = "effect_unknown"


class ReceiptOutcome(str, Enum):
    CONFIRMED_FAILURE_BEFORE_EFFECT = "confirmed_failure_before_effect"
    CONFIRMED_SUCCESS = "confirmed_success"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WaitConditionType(str, Enum):
    NAVIGATION_COMMIT = "navigation_commit"
    DOCUMENT_EPOCH_CHANGE = "document_epoch_change"
    URL_CHANGE = "url_change"
    URL_MATCH = "url_match"
    DOM_SELECTOR_STATE = "dom_selector_state"
    ELEMENT_STATE = "element_state"
    TEXT_APPEARANCE = "text_appearance"
    TEXT_DISAPPEARANCE = "text_disappearance"
    POPUP_CREATION = "popup_creation"
    DIALOG_APPEARANCE = "dialog_appearance"
    DIALOG_CLOSURE = "dialog_closure"
    DOWNLOAD_START = "download_start"
    DOWNLOAD_COMPLETION = "download_completion"
    NETWORK_QUIET = "network_quiet"
    STABLE_CONTENT = "stable_content_fingerprint"
    CUSTOM_POSTCONDITION = "custom_postcondition"


class WaitFailurePolicy(str, Enum):
    CONFIRMED_FAILURE = "confirmed_failure"
    OUTCOME_UNKNOWN_IF_DISPATCHED = "outcome_unknown_if_dispatched"


@dataclass(frozen=True)
class WaitSpec:
    condition_type: WaitConditionType
    expected: Any = None
    timeout_seconds: float = 10.0
    quiet_period_seconds: float = 0.5
    failure_policy: WaitFailurePolicy = WaitFailurePolicy.OUTCOME_UNKNOWN_IF_DISPATCHED

    @classmethod
    def parse(cls, value: Mapping[str, Any] | "WaitSpec") -> "WaitSpec":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("invalid_wait_spec")
        try:
            condition = WaitConditionType(str(value.get("condition_type") or ""))
            timeout = float(value.get("timeout_seconds", 10.0))
            quiet = float(value.get("quiet_period_seconds", 0.5))
            failure = WaitFailurePolicy(str(
                value.get("failure_policy")
                or WaitFailurePolicy.OUTCOME_UNKNOWN_IF_DISPATCHED.value
            ))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid_wait_spec") from exc
        if not (0.05 <= timeout <= MAX_WAIT_SECONDS):
            raise ValueError("invalid_wait_timeout")
        if not (0.05 <= quiet <= min(timeout, 10.0)):
            raise ValueError("invalid_wait_quiet_period")
        expected = _safe_expected(value.get("expected"), condition=condition)
        if condition == WaitConditionType.CUSTOM_POSTCONDITION:
            nested_total = sum(
                float(item.get("timeout_seconds", 0)) for item in expected
            )
            if nested_total > timeout:
                raise ValueError("custom_postcondition_timeout_exceeded")
        return cls(condition, expected, timeout, quiet, failure)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_type": self.condition_type.value,
            "expected": self.expected,
            "timeout_seconds": self.timeout_seconds,
            "quiet_period_seconds": self.quiet_period_seconds,
            "failure_policy": self.failure_policy.value,
        }


def _safe_expected(value: Any, *, condition: WaitConditionType) -> Any:
    if condition == WaitConditionType.CUSTOM_POSTCONDITION:
        if not isinstance(value, list) or not (1 <= len(value) <= 8):
            raise ValueError("invalid_custom_postcondition")
        out = []
        for item in value:
            nested = WaitSpec.parse(item)
            if nested.condition_type == WaitConditionType.CUSTOM_POSTCONDITION:
                raise ValueError("nested_custom_postcondition")
            out.append(nested.to_dict())
        return out
    if condition == WaitConditionType.DOM_SELECTOR_STATE:
        if not isinstance(value, Mapping) or set(value) != {"primitive", "state"}:
            raise ValueError("invalid_dom_primitive")
        primitive = str(value.get("primitive") or "")
        state = str(value.get("state") or "")
        if primitive not in {"main", "article", "dialog", "form", "table", "list"}:
            raise ValueError("invalid_dom_primitive")
        if state not in {"attached", "visible", "hidden"}:
            raise ValueError("invalid_dom_state")
        return {"primitive": primitive, "state": state}
    if condition == WaitConditionType.ELEMENT_STATE:
        if not isinstance(value, Mapping) or set(value) != {"element_ref", "state"}:
            raise ValueError("invalid_element_state")
        element_ref = str(value.get("element_ref") or "")
        state = str(value.get("state") or "")
        if not _OPAQUE_RE.fullmatch(element_ref) or not element_ref.startswith("el_"):
            raise ValueError("invalid_element_reference")
        if state not in {
            "visible", "hidden", "enabled", "disabled", "checked",
            "selected", "expanded", "collapsed",
        }:
            raise ValueError("invalid_element_state")
        return {"element_ref": element_ref, "state": state}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_SAFE_EXPECTED_TEXT:
            raise ValueError("wait_expected_too_large")
        return value
    if isinstance(value, Mapping):
        encoded = json.dumps(dict(value), sort_keys=True, ensure_ascii=False)
        if len(encoded) > MAX_SAFE_EXPECTED_TEXT:
            raise ValueError("wait_expected_too_large")
        return json.loads(encoded)
    raise ValueError("invalid_wait_expected")


@dataclass(frozen=True)
class BrowserStateRef:
    schema_version: int
    browser_session_id: str
    context_id: str
    context_epoch: int
    page_id: str
    page_generation: int
    navigation_id: str
    document_epoch: int
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "browser_session_id": self.browser_session_id,
            "context_id": self.context_id,
            "context_epoch": self.context_epoch,
            "page_id": self.page_id,
            "page_generation": self.page_generation,
            "navigation_id": self.navigation_id,
            "document_epoch": self.document_epoch,
            "created_at": self.created_at,
        }

    @classmethod
    def parse(cls, value: Mapping[str, Any] | "BrowserStateRef") -> "BrowserStateRef":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise BrowserReferenceError()
        try:
            ref = cls(
                schema_version=int(value["schema_version"]),
                browser_session_id=str(value["browser_session_id"]),
                context_id=str(value["context_id"]),
                context_epoch=int(value["context_epoch"]),
                page_id=str(value["page_id"]),
                page_generation=int(value["page_generation"]),
                navigation_id=str(value["navigation_id"]),
                document_epoch=int(value["document_epoch"]),
                created_at=float(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise BrowserReferenceError() from exc
        if (
            ref.schema_version != BROWSER_STATE_SCHEMA_VERSION
            or ref.context_epoch < 1
            or ref.page_generation < 1
            or ref.document_epoch < 0
            or not all(_OPAQUE_RE.fullmatch(item) for item in (
                ref.browser_session_id, ref.context_id, ref.page_id,
                ref.navigation_id,
            ))
        ):
            raise BrowserReferenceError()
        return ref


@dataclass(frozen=True)
class ActionReceipt:
    operation_id: str
    tool_call_id: str
    before_state: BrowserStateRef | None
    after_state: BrowserStateRef | None
    dispatch_state: DispatchState
    outcome: ReceiptOutcome
    evidence: tuple[dict[str, Any], ...]
    started_at: float
    completed_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_RECEIPT_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "tool_call_id": self.tool_call_id,
            "before_state": self.before_state.to_dict() if self.before_state else None,
            "after_state": self.after_state.to_dict() if self.after_state else None,
            "dispatch_state": self.dispatch_state.value,
            "outcome": self.outcome.value,
            "evidence": [dict(item) for item in self.evidence[:16]],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def make_receipt(
    *,
    before_state: BrowserStateRef | None,
    after_state: BrowserStateRef | None,
    dispatch_state: DispatchState,
    outcome: ReceiptOutcome,
    evidence: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    started_at: float | None = None,
    operation_id: str | None = None,
    tool_call_id: str | None = None,
) -> ActionReceipt:
    return ActionReceipt(
        operation_id=operation_id or opaque_ref("op"),
        tool_call_id=tool_call_id or opaque_ref("call"),
        before_state=before_state,
        after_state=after_state,
        dispatch_state=dispatch_state,
        outcome=outcome,
        evidence=tuple(_bounded_evidence(evidence)),
        started_at=float(started_at if started_at is not None else time.time()),
        completed_at=time.time(),
    )


def _bounded_evidence(values) -> list[dict[str, Any]]:
    out = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        clean: dict[str, Any] = {}
        for key, value in list(item.items())[:12]:
            safe_key = re.sub(r"[^a-z0-9_]", "", str(key).lower())[:48]
            if not safe_key:
                continue
            if isinstance(value, str):
                clean[safe_key] = value[:512]
            elif value is None or isinstance(value, (bool, int, float)):
                clean[safe_key] = value
        if clean:
            out.append(clean)
        if len(out) >= 16:
            break
    return out


def action_result(
    *,
    ok: bool,
    receipt: ActionReceipt,
    data: Mapping[str, Any] | None = None,
    error_code: str = "",
    text: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": bool(ok),
        "receipt": receipt.to_dict(),
        "text": text[:2_000] if text else (
            "Browser operation completed with verified evidence."
            if ok else f"ERROR: browser operation failed ({error_code or 'browser_error'})"
        ),
    }
    if data:
        result["data"] = dict(data)
    if not ok:
        result["error"] = {"code": error_code or "browser_error"}
    return result


@dataclass
class _PageIdentity:
    page: Any
    page_id: str
    page_generation: int
    navigation_id: str
    document_epoch: int
    created_at: float
    opener_page_id: str = ""


class BrowserStateTracker:
    """Owner-thread registry mapping private page objects to opaque references."""

    def __init__(self, *, clock=None):
        self._clock = clock or time.time
        self._browser_session_id = ""
        self._context_id = ""
        self._context_epoch = 0
        self._context = None
        self._pages: dict[int, _PageIdentity] = {}
        self._page_ids: dict[str, _PageIdentity] = {}
        self._retired_page_ids: set[str] = set()
        self._events: list[dict[str, Any]] = []
        self._event_sequence = 0

    def start_context(self, context: Any, *, reason: str = "context_started") -> None:
        self.retire_all(reason="context_replaced")
        self._browser_session_id = opaque_ref("browser")
        self._context_id = opaque_ref("context")
        self._context_epoch += 1
        self._context = context
        self._event(reason)

    def retire_all(self, *, reason: str) -> None:
        for identity in self._pages.values():
            self._retired_page_ids.add(identity.page_id)
        self._pages.clear()
        self._page_ids.clear()
        self._context = None
        if self._browser_session_id:
            self._event(reason)

    def register_page(self, page: Any, *, opener: Any = None, popup=False) -> BrowserStateRef:
        key = id(page)
        identity = self._pages.get(key)
        if identity is None:
            opener_identity = self._pages.get(id(opener)) if opener is not None else None
            identity = _PageIdentity(
                page=page,
                page_id=opaque_ref("page"),
                page_generation=1,
                navigation_id=opaque_ref("nav"),
                document_epoch=0,
                created_at=self._clock(),
                opener_page_id=opener_identity.page_id if opener_identity else "",
            )
            self._pages[key] = identity
            self._page_ids[identity.page_id] = identity
            self._event("popup_created" if popup else "page_created", identity)
        return self._ref(identity)

    def note_navigation(self, page: Any, *, reason: str = "navigation") -> BrowserStateRef:
        identity = self._pages.get(id(page))
        if identity is None:
            return self.register_page(page)
        identity.navigation_id = opaque_ref("nav")
        identity.document_epoch += 1
        self._event(reason, identity)
        return self._ref(identity)

    def set_opener(self, page: Any, opener: Any) -> None:
        identity = self._pages.get(id(page))
        opener_identity = self._pages.get(id(opener))
        if identity is not None and opener_identity is not None:
            identity.opener_page_id = opener_identity.page_id

    def retire_page(self, page: Any, *, reason: str = "page_closed") -> None:
        identity = self._pages.pop(id(page), None)
        if identity is None:
            return
        self._page_ids.pop(identity.page_id, None)
        self._retired_page_ids.add(identity.page_id)
        self._event(reason, identity)

    def current(self, page: Any) -> BrowserStateRef:
        identity = self._pages.get(id(page))
        if identity is None:
            raise BrowserReferenceError()
        return self._ref(identity)

    def validate(self, value: Mapping[str, Any] | BrowserStateRef) -> Any:
        ref = BrowserStateRef.parse(value)
        if (
            ref.browser_session_id != self._browser_session_id
            or ref.context_id != self._context_id
            or ref.context_epoch != self._context_epoch
        ):
            raise BrowserReferenceError()
        identity = self._page_ids.get(ref.page_id)
        if identity is None:
            raise BrowserReferenceError()
        current = self._ref(identity)
        if ref != current:
            raise BrowserReferenceError()
        return identity.page

    def tab_refs(self) -> list[tuple[Any, BrowserStateRef]]:
        return [(item.page, self._ref(item)) for item in self._pages.values()]

    def opener(self, page: Any) -> Any | None:
        identity = self._pages.get(id(page))
        if identity is None or not identity.opener_page_id:
            return None
        opener = self._page_ids.get(identity.opener_page_id)
        return opener.page if opener is not None else None

    def events(self, *, after=0) -> list[dict[str, Any]]:
        start = max(0, int(after))
        return [dict(item) for item in self._events[start:]][:MAX_LIFECYCLE_EVENTS]

    def _ref(self, identity: _PageIdentity) -> BrowserStateRef:
        return BrowserStateRef(
            schema_version=BROWSER_STATE_SCHEMA_VERSION,
            browser_session_id=self._browser_session_id,
            context_id=self._context_id,
            context_epoch=self._context_epoch,
            page_id=identity.page_id,
            page_generation=identity.page_generation,
            navigation_id=identity.navigation_id,
            document_epoch=identity.document_epoch,
            created_at=identity.created_at,
        )

    def _event(self, event_type: str, identity: _PageIdentity | None = None) -> None:
        self._event_sequence += 1
        event = {
            "schema_version": 1,
            "event_id": opaque_ref("event"),
            "sequence": self._event_sequence,
            "event_type": re.sub(r"[^a-z0-9_]", "", str(event_type).lower())[:64],
            "browser_session_id": self._browser_session_id,
            "context_id": self._context_id,
            "context_epoch": self._context_epoch,
            "created_at": self._clock(),
        }
        if identity is not None:
            event.update({
                "page_id": identity.page_id,
                "page_generation": identity.page_generation,
                "document_epoch": identity.document_epoch,
            })
        self._events.append(event)
        if len(self._events) > MAX_LIFECYCLE_EVENTS:
            self._events = self._events[-MAX_LIFECYCLE_EVENTS:]


@dataclass(frozen=True)
class BoundReference:
    ref_id: str
    kind: str
    state: BrowserStateRef
    payload: Any
    created_at: float


class BoundReferenceStore:
    """Short-lived opaque element/frame/result references bound to one document."""

    def __init__(self, *, maximum=512, ttl_seconds=600.0, clock=None):
        self._maximum = max(16, min(int(maximum), 2_048))
        self._ttl = max(5.0, min(float(ttl_seconds), 3_600.0))
        self._clock = clock or time.time
        self._items: dict[str, BoundReference] = {}
        self._bindings: dict[tuple[BrowserStateRef, str, str], str] = {}

    def issue(
        self,
        kind: str,
        state: BrowserStateRef,
        payload: Any,
        *,
        binding_key: str = "",
    ) -> str:
        self.purge()
        normalized_kind = str(kind)[:48]
        binding = None
        if binding_key:
            binding = (state, normalized_kind, str(binding_key)[:128])
            existing_ref = self._bindings.get(binding)
            existing = self._items.get(existing_ref or "")
            if existing is not None:
                self._items[existing.ref_id] = BoundReference(
                    existing.ref_id,
                    normalized_kind,
                    state,
                    payload,
                    self._clock(),
                )
                return existing.ref_id
        ref_id = opaque_ref("el")
        item = BoundReference(ref_id, normalized_kind, state, payload, self._clock())
        self._items[ref_id] = item
        if binding is not None:
            self._bindings[binding] = ref_id
        while len(self._items) > self._maximum:
            self._drop(next(iter(self._items)))
        return ref_id

    def resolve(self, ref_id: str, state: BrowserStateRef, *, kinds=()) -> Any:
        self.purge()
        item = self._items.get(str(ref_id or ""))
        if (
            item is None
            or item.state != state
            or (kinds and item.kind not in set(kinds))
        ):
            raise BrowserReferenceError("stale_element_reference")
        return item.payload

    def invalidate_page(self, page_id: str) -> None:
        for key, item in tuple(self._items.items()):
            if item.state.page_id == page_id:
                self._drop(key)

    def clear(self) -> None:
        self._items.clear()
        self._bindings.clear()

    def purge(self) -> None:
        cutoff = self._clock() - self._ttl
        for key, item in tuple(self._items.items()):
            if item.created_at < cutoff:
                self._drop(key)

    def _drop(self, ref_id: str) -> None:
        self._items.pop(ref_id, None)
        for binding, bound_ref in tuple(self._bindings.items()):
            if bound_ref == ref_id:
                self._bindings.pop(binding, None)


def content_fingerprint(value: Any) -> str:
    text = " ".join(str(value or "").split())[:200_000]
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


__all__ = [
    "ActionReceipt", "BoundReferenceStore", "BrowserReferenceError",
    "BrowserStateRef", "BrowserStateTracker", "DispatchState",
    "ReceiptOutcome", "WaitConditionType", "WaitFailurePolicy", "WaitSpec",
    "action_result", "content_fingerprint", "make_receipt", "opaque_ref",
    "safe_origin", "safe_url_display", "scrub_browser_error",
]
