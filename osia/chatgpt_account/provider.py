"""OSIA provider adapter for managed ChatGPT authentication via Codex."""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from osia.config import get_setting, set_setting

from .app_server_client import AppServerClient, AppServerError, TurnResult


ACCOUNT_PROVIDER_ID = "chatgpt_account"
ACCOUNT_PROVIDER_NAME = "ChatGPT account"

_ACCOUNT_CHANGE_LOCK = threading.Lock()
_ACCOUNT_CHANGE_LISTENERS: set[Callable[[], None]] = set()

_CONTAINMENT_INSTRUCTIONS = """

CHATGPT ACCOUNT PROVIDER CONTAINMENT AND CAPABILITY FEDERATION:
- Provider-hosted tools and listed OSIA calls are both available. OSIA calls are
  optional additions, never required substitutes; choose the best valid path.
- Request native local command/file approval normally. OSIA applies the same
  permission broker used by OSIA tools and may pause before the effect.
- This federation does NOT prohibit benign tasks or delegation; delegate through
  OSIA when it is the best valid path.
- Once any native or OSIA mutation is accepted or started, never repeat it through
  an equivalent mechanism. Switch only after a proven no-effect outcome permits it.
- Do not generically refuse benign tasks. Follow the base action policy and claim
  actions only from observed results.
- Do not mention or infer local paths unless the current request requires them.
"""

_DEVELOPER_INSTRUCTIONS = (
    "Act as OSIA's provider. Follow base instructions and capability federation. "
    "Native hosted tools and optional OSIA calls are available; choose one path "
    "and report only observed outcomes."
)

_LANGUAGE_MARKER = "\n\n[AUTHORITATIVE TURN LANGUAGE]\n"


class AccountProviderError(RuntimeError):
    """Provider error carrying the no-duplicate-fallback boundary."""

    def __init__(
        self,
        error: AppServerError,
        *,
        selected_model: str | None = None,
        reasoning_level: str | None = None,
    ):
        super().__init__(str(error))
        self.output_started = error.output_started
        self.native_action = error.native_action
        self.controlled_refusal = error.controlled_refusal
        self.native_tool_call = (
            dict(error.native_tool_call)
            if isinstance(error.native_tool_call, dict)
            else None
        )
        self.reformulatable_native_action = error.reformulatable_native_action
        self.allow_fallback = error.allow_fallback
        self.category = error.category
        self.method = error.method
        self.server_code = error.server_code
        self.capability_events = tuple(
            getattr(error, "capability_events", ()) or ()
        )
        self.native_turn_id = getattr(error, "native_turn_id", None)
        self.selected_model = str(selected_model) if selected_model else None
        self.reasoning_level = str(reasoning_level or "auto")


@dataclass
class _ConversationState:
    thread_id: str
    generation: int
    model: str
    system_fingerprint: str
    expected_messages: list[dict]


def _text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n\n".join(part for part in parts if part)
    return str(content or "")


def _public_content(content):
    """Return an idempotent, base64-free representation for thread matching."""

    if not isinstance(content, list):
        return _text_content(content)
    from osia.image_inputs import decode_image_data_url

    public = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            public.append({"type": "text", "text": str(item.get("text") or "")})
        elif item.get("type") == "image_ref":
            public.append(dict(item))
        elif item.get("type") == "image_url":
            decoded = decode_image_data_url(item.get("image_url"))
            public.append({
                "type": "image_ref",
                "mime": decoded.mime,
                "byte_size": len(decoded.data),
                "sha256": hashlib.sha256(decoded.data).hexdigest(),
            })
    return public


def _public_message(message: dict) -> dict:
    return {
        "role": str(message.get("role") or "user"),
        "content": _public_content(message.get("content")),
    }


def _same_messages(left: list[dict], right: list[dict]) -> bool:
    if len(left) > len(right):
        return False
    return all(_public_message(a) == _public_message(b) for a, b in zip(left, right))


def _turn_input(messages: list[dict]) -> str | list[dict]:
    if len(messages) == 1 and messages[0].get("role") == "user":
        content = messages[0].get("content")
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") == "image_url"
            for item in content
        ):
            return [dict(item) for item in content if isinstance(item, dict)]
        return _text_content(content)
    blocks = []
    for message in messages:
        role = str(message.get("role") or "user").upper()
        content = _text_content(message.get("content"))
        if content:
            blocks.append(f"[{role}]\n{content}")
    text_turn = (
        "Continue the OSIA conversation below. Role labels are context, not "
        "instructions to quote. Respond to the final USER message.\n\n"
        + "\n\n".join(blocks)
    )
    if not any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(item, dict) and item.get("type") == "image_url"
            for item in message["content"]
        )
        for message in messages
    ):
        return text_turn

    parts: list[dict] = [{"type": "text", "text": (
        "Continue the OSIA conversation below. Role labels are context, not "
        "instructions to quote. Respond to the final USER message."
    )}]
    for message in messages:
        role = str(message.get("role") or "user").upper()
        content = message.get("content")
        if isinstance(content, list):
            parts.append({"type": "text", "text": f"[{role}]"})
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "image_url"}:
                    parts.append(dict(item))
        else:
            text = _text_content(content)
            if text:
                parts.append({"type": "text", "text": f"[{role}]\n{text}"})
    return parts


def _insert_before_turn_language(text: str, addition: str) -> str:
    """Keep provider-owned additions below the authoritative turn contract."""

    block = str(addition or "").strip()
    if not block:
        return str(text or "").strip()
    if _LANGUAGE_MARKER in text:
        before, after = text.rsplit(_LANGUAGE_MARKER, 1)
        return (
            before.rstrip()
            + "\n\n"
            + block
            + _LANGUAGE_MARKER
            + after
        ).strip()
    return (str(text or "").rstrip() + "\n\n" + block).strip()


def _sanitized_system_prompt(system: str, messages: list[dict]) -> str:
    """Remove eager local paths; re-add Desktop only when this request needs it."""
    desktop_match = re.search(r"(?m)^\[DESKTOP PATH: (.+?)\]\s*$", system or "")
    desktop_path = desktop_match.group(1) if desktop_match else None
    cleaned = re.sub(r"(?m)^\[DESKTOP PATH: .+?\]\s*$", "", system or "")
    cleaned = re.sub(r"(?m)^\[PROJECT ROOT: .+?\]\s*$", "", cleaned)
    cleaned = cleaned.replace(
        'When the user says "desktop" or "bureau", use the exact path in the '
        '[DESKTOP PATH: ...] line.',
        "Use a local desktop path only when the current request requires it.",
    )
    latest_user = next(
        (_text_content(message.get("content")) for message in reversed(messages)
         if message.get("role") == "user"),
        "",
    )
    if desktop_path and re.search(r"\b(desktop|bureau)\b", latest_user, re.IGNORECASE):
        cleaned = _insert_before_turn_language(
            cleaned, f"[DESKTOP PATH: {desktop_path}]",
        )
    return cleaned.strip()


def _bounded_turn_timeout(value) -> float:
    """Preserve the account provider's existing 300 s maximum."""
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError):
        return 300.0
    if not math.isfinite(timeout) or timeout <= 0:
        return 300.0
    return min(timeout, 300.0)


def _canonical_native_tool_text(error: AppServerError) -> str | None:
    """Serialize one exact App Server handoff into OSIA's existing wire form."""

    handoff = error.native_tool_call
    if not isinstance(handoff, dict):
        return None
    tool = handoff.get("tool")
    arguments = handoff.get("arguments")
    call_id = handoff.get("call_id")
    if not isinstance(tool, str) or not tool:
        return None
    if not isinstance(arguments, dict) or "tool" in arguments:
        return None
    if not isinstance(call_id, str) or not call_id:
        return None
    payload = {"tool": tool, **arguments}
    return (
        "<tool_call>\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n</tool_call>"
    )


class ChatGPTAccountProvider:
    def __init__(self, client: AppServerClient | None = None):
        self.client = client or AppServerClient()
        self._lock = threading.RLock()
        self._models: list[dict] = []
        self._threads: dict[str, _ConversationState] = {}

    def status(self, *, refresh_models: bool = True) -> dict:
        try:
            health = self.client.health_check()
            account = self.client.get_account_status()
            models = self.list_models(force=True) if account["connected"] and refresh_models else list(self._models)
            selected = self.selected_model(models)
            feature_enabled = bool(get_setting("chatgpt_account_feature_enabled", True))
            return {
                "available": bool(feature_enabled and account["connected"] and selected),
                "runtimeAvailable": True,
                "runtimeVersion": health.get("runtimeVersion"),
                "connected": bool(account["connected"]),
                "account": account.get("account"),
                "models": models,
                "selectedModel": selected,
                "error": (
                    "" if feature_enabled and account["connected"]
                    else "feature_disabled" if not feature_enabled
                    else "disconnected"
                ),
            }
        except AppServerError as exc:
            return {
                "available": False,
                "runtimeAvailable": exc.category not in {"runtime_missing", "runtime_incompatible"},
                "runtimeVersion": self.client.get_runtime_version(),
                "connected": False,
                "account": None,
                "models": [],
                "selectedModel": None,
                "error": exc.category,
            }

    def list_models(self, *, force: bool = False) -> list[dict]:
        with self._lock:
            if self._models and not force:
                return [dict(model) for model in self._models]
        models = self.client.list_models()
        with self._lock:
            self._models = [dict(model) for model in models]
            return [dict(model) for model in self._models]

    def cached_models(self) -> list[dict]:
        with self._lock:
            return [dict(model) for model in self._models]

    def selected_model(self, models: list[dict] | None = None) -> str | None:
        models = models if models is not None else self.list_models()
        valid = {str(model.get("id")) for model in models if model.get("id")}
        selected = str(get_setting("chatgpt_account_model", "") or "").strip()
        if selected in valid:
            return selected
        default = next((m for m in models if m.get("isDefault")), None)
        chosen = default or (models[0] if models else None)
        return str(chosen.get("id")) if chosen else None

    def set_selected_model(self, model_id: str) -> None:
        valid = {str(model.get("id")) for model in self.list_models() if model.get("id")}
        if model_id not in valid:
            raise AppServerError("ERROR: that ChatGPT account model is unavailable.", category="model_unavailable")
        set_setting("chatgpt_account_model", model_id)
        with self._lock:
            self._threads.clear()

    def begin_login(self) -> dict:
        return self.client.begin_login()

    def begin_device_code_login(self) -> dict:
        return self.client.begin_device_code_login()

    def wait_for_login(self, login_id: str, timeout: float = 300.0) -> dict:
        status = self.client.wait_for_login(login_id, timeout=timeout)
        if status.get("connected"):
            self.list_models(force=True)
        _notify_account_changed()
        return status

    def cancel_login(self) -> None:
        self.client.cancel_login()
        _notify_account_changed()

    def reset_login(self) -> None:
        self.client.reset_login()

    def logout(self) -> None:
        with self._lock:
            states = list(self._threads.values())
        for state in states:
            self.client.cancel_turn(thread_id=state.thread_id)
        self.client.logout()
        with self._lock:
            self._models.clear()
            self._threads.clear()
        _notify_account_changed()

    def rate_limits(self) -> dict:
        return self.client.get_rate_limits()

    def chat_client(self, conversation_id: str | None = None):
        return _AccountChatClient(self, conversation_id=conversation_id)

    def _prepare(
        self, conversation_id: str, model: str, messages: list[dict]
    ) -> tuple[_ConversationState, list[dict]]:
        public = [_public_message(message) for message in messages]
        system = next((m["content"] for m in public if m["role"] == "system"), "")
        system = _sanitized_system_prompt(system, public)
        base_instructions = _insert_before_turn_language(
            system, _CONTAINMENT_INSTRUCTIONS,
        )
        system_fingerprint = hashlib.sha256(system.encode("utf-8")).hexdigest()
        public_non_system = [m for m in public if m["role"] != "system"]
        non_system = [message for message in messages if message.get("role") != "system"]
        with self._lock:
            state = self._threads.get(conversation_id)
            reusable = bool(
                state
                and state.generation == self.client.generation
                and state.model == model
                and state.system_fingerprint == system_fingerprint
                and _same_messages(state.expected_messages, public_non_system)
            )
            delta = non_system[len(state.expected_messages):] if reusable and state else non_system
        if not reusable:
            thread_id = self.client.start_or_resume_conversation(
                thread_id=None,
                model=model,
                base_instructions=base_instructions,
                developer_instructions=_DEVELOPER_INSTRUCTIONS,
            )
            state = _ConversationState(
                thread_id=thread_id,
                generation=self.client.generation,
                model=model,
                system_fingerprint=system_fingerprint,
                expected_messages=[],
            )
            delta = non_system
            with self._lock:
                self._threads[conversation_id] = state
        if not delta:
            raise AppServerError("ERROR: no new OSIA message was available for ChatGPT.", category="empty_input")
        return state, delta

    def complete(
        self,
        *,
        conversation_id: str,
        model: str,
        messages: list[dict],
        osia_turn_id: str | None = None,
        client_request_id: str | None = None,
        final_response_message_id: str | None = None,
        effort: str | None = None,
        on_delta=None,
        timeout: float = 300.0,
    ) -> TurnResult:
        state = None
        try:
            state, delta_messages = self._prepare(conversation_id, model, messages)
            turn_input = _turn_input(delta_messages)
            run_kwargs = dict(
                thread_id=state.thread_id,
                conversation_id=conversation_id,
                osia_turn_id=osia_turn_id,
                client_request_id=client_request_id,
                final_response_message_id=final_response_message_id,
                model=model,
                effort=effort,
                on_delta=on_delta,
                timeout=_bounded_turn_timeout(timeout),
            )
            if isinstance(turn_input, list):
                run_kwargs["content"] = turn_input
            else:
                run_kwargs["text"] = turn_input
            result = self.client.run_turn(**run_kwargs)
            with self._lock:
                current = self._threads.get(conversation_id)
                if current is state:
                    current.expected_messages = [
                        _public_message(message)
                        for message in messages
                        if message.get("role") != "system"
                    ] + [{"role": "assistant", "content": result.text}]
            return result
        except AppServerError as exc:
            native_tool_text = _canonical_native_tool_text(exc)
            if native_tool_text is not None and state is not None:
                if on_delta is not None:
                    on_delta(native_tool_text)
                result = TurnResult(
                    text=native_tool_text,
                    thread_id=state.thread_id,
                    turn_id=None,
                    status="interrupted",
                    output_started=exc.output_started,
                    capability_events=tuple(
                        getattr(exc, "capability_events", ()) or ()
                    ),
                )
                with self._lock:
                    current = self._threads.get(conversation_id)
                    if current is state:
                        current.expected_messages = [
                            _public_message(message)
                            for message in messages
                            if message.get("role") != "system"
                        ] + [{"role": "assistant", "content": result.text}]
                return result
            if exc.native_action and state is not None:
                with self._lock:
                    current = self._threads.get(conversation_id)
                    if current is state:
                        current.expected_messages = [
                            _public_message(message)
                            for message in messages
                            if message.get("role") != "system"
                        ]
            if exc.category in {"thread_failed", "request_rejected", "process_crash"}:
                with self._lock:
                    self._threads.pop(conversation_id, None)
            raise AccountProviderError(
                exc,
                selected_model=model,
                reasoning_level=effort,
            ) from exc

    def cancel_conversation(self, conversation_id: str) -> None:
        with self._lock:
            state = self._threads.get(conversation_id)
        if state:
            self.client.cancel_turn(thread_id=state.thread_id)

    def shutdown(self) -> None:
        self.client.shutdown()
        with self._lock:
            self._threads.clear()


class _AccountCompletions:
    def __init__(self, owner: "_AccountChatClient"):
        self._owner = owner

    def create(self, *, model: str, messages: list[dict], stream: bool = False, **kwargs):
        self._owner._set_capability_events(())
        self._owner._set_invocation_metadata({})
        conversation_id = self._owner.conversation_id or f"ephemeral:{id(self._owner)}"
        effort = kwargs.get("effort")
        timeout = _bounded_turn_timeout(kwargs.get("timeout", 300.0))
        if not stream:
            try:
                result = self._owner.provider.complete(
                    conversation_id=conversation_id,
                    model=model,
                    messages=messages,
                    osia_turn_id=self._owner.osia_turn_id,
                    client_request_id=self._owner.client_request_id,
                    final_response_message_id=(
                        self._owner.final_response_message_id
                    ),
                    effort=effort,
                    timeout=timeout,
                )
            except Exception as exc:
                self._owner._preserve_exception_state(exc)
                raise
            self._owner._set_capability_events(
                getattr(result, "capability_events", ()),
            )
            self._owner._set_invocation_metadata({
                "native_turn_id": getattr(result, "turn_id", None),
                "native_thread_id": getattr(result, "thread_id", None),
                "native_status": getattr(result, "status", ""),
                "output_started": bool(getattr(result, "output_started", False)),
            })
            message = SimpleNamespace(content=result.text, tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        events: queue.Queue = queue.Queue()

        def worker():
            try:
                result = self._owner.provider.complete(
                    conversation_id=conversation_id,
                    model=model,
                    messages=messages,
                    osia_turn_id=self._owner.osia_turn_id,
                    client_request_id=self._owner.client_request_id,
                    final_response_message_id=(
                        self._owner.final_response_message_id
                    ),
                    effort=effort,
                    on_delta=lambda delta: events.put(("delta", delta)),
                    timeout=timeout,
                )
                self._owner._set_capability_events(
                    getattr(result, "capability_events", ()),
                )
                self._owner._set_invocation_metadata({
                    "native_turn_id": getattr(result, "turn_id", None),
                    "native_thread_id": getattr(result, "thread_id", None),
                    "native_status": getattr(result, "status", ""),
                    "output_started": bool(getattr(result, "output_started", False)),
                })
                events.put(("done", None))
            except Exception as exc:
                self._owner._preserve_exception_state(exc)
                events.put(("error", exc))

        # Streaming uses a dedicated provider thread.  Carry the originating
        # server turn context into it so a native approval reaches the same
        # canonical permission presenter as OSIA and MCP tool calls.
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run,
            args=(worker,),
            name="osia-chatgpt-turn",
            daemon=True,
        ).start()

        def iterator():
            while True:
                kind, payload = events.get()
                if kind == "delta":
                    delta = SimpleNamespace(content=str(payload))
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])
                elif kind == "error":
                    raise payload
                else:
                    return

        return iterator()


class _AccountChatClient:
    def __init__(self, provider: ChatGPTAccountProvider, conversation_id: str | None = None):
        self.provider = provider
        self.conversation_id = conversation_id
        self.osia_turn_id: str | None = None
        self.client_request_id: str | None = None
        self.final_response_message_id: str | None = None
        self._capability_events: tuple = ()
        self._capability_events_lock = threading.Lock()
        self._invocation_metadata: dict = {}
        self._invocation_metadata_lock = threading.Lock()
        self.chat = SimpleNamespace(completions=_AccountCompletions(self))

    def _set_capability_events(self, events) -> None:
        with self._capability_events_lock:
            self._capability_events = tuple(events or ())

    def _preserve_exception_state(self, exc: Exception) -> None:
        self._set_capability_events(
            getattr(exc, "capability_events", ()) or ()
        )
        self._set_invocation_metadata({
            "native_turn_id": getattr(exc, "native_turn_id", None),
            "native_status": "failed",
            "output_started": bool(getattr(exc, "output_started", False)),
        })

    def consume_capability_events(self) -> tuple:
        """Return provider-native events from exactly one completed model call."""

        with self._capability_events_lock:
            events = self._capability_events
            self._capability_events = ()
        return events

    def _set_invocation_metadata(self, metadata) -> None:
        with self._invocation_metadata_lock:
            self._invocation_metadata = dict(metadata or {})

    def consume_invocation_metadata(self) -> dict:
        """Return native identity/status for one completed provider segment."""

        with self._invocation_metadata_lock:
            metadata = dict(self._invocation_metadata)
            self._invocation_metadata = {}
        return metadata

    def set_conversation_id(self, conversation_id: str | None) -> None:
        self.conversation_id = conversation_id

    def set_tool_attempt_turn_id(self, turn_id: str | None) -> None:
        self.osia_turn_id = str(turn_id) if turn_id else None

    def set_tool_attempt_request_id(self, request_id: str | None) -> None:
        self.client_request_id = str(request_id) if request_id else None

    def set_tool_attempt_final_response_message_id(
        self, message_id: str | None,
    ) -> None:
        self.final_response_message_id = str(message_id) if message_id else None


_PROVIDER: ChatGPTAccountProvider | None = None
_PROVIDER_LOCK = threading.Lock()


def add_account_change_listener(callback: Callable[[], None]) -> Callable[[], None]:
    """Subscribe to account changes without exposing App Server payloads."""
    with _ACCOUNT_CHANGE_LOCK:
        _ACCOUNT_CHANGE_LISTENERS.add(callback)

    def unsubscribe() -> None:
        with _ACCOUNT_CHANGE_LOCK:
            _ACCOUNT_CHANGE_LISTENERS.discard(callback)

    return unsubscribe


def _notify_account_changed() -> None:
    with _ACCOUNT_CHANGE_LOCK:
        callbacks = tuple(_ACCOUNT_CHANGE_LISTENERS)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            pass


def get_account_provider() -> ChatGPTAccountProvider:
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = ChatGPTAccountProvider()
        return _PROVIDER


def get_cached_account_models() -> list[dict]:
    return get_account_provider().cached_models()
