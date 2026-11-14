"""OSIA provider adapter for managed ChatGPT authentication via Codex."""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import queue
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import SimpleNamespace

from osia.config import get_setting, set_setting

from .app_server_client import (
    AppServerClient,
    AppServerError,
    TurnResult,
    _canonical_native_tools,
)


ACCOUNT_PROVIDER_ID = "chatgpt_account"
ACCOUNT_PROVIDER_NAME = "ChatGPT account"

_ACCOUNT_CHANGE_LOCK = threading.Lock()
_ACCOUNT_CHANGE_LISTENERS: set[Callable[[], None]] = set()

_CONTAINMENT_INSTRUCTIONS = """

[ChatGPT account capability context: provider-hosted and listed OSIA
capabilities are both available. The same permission, ActionTruth,
and evidence rules apply to either path. A capability exposed as a native
dynamic tool is invoked only through that native call, never duplicated as an
XML/text tool call. A connected MCP tool exposed natively has an
`osia_mcp__` alias for the corresponding `mcp__` tool in the inventory.
Do not infer local paths unless needed.]
"""

_DEVELOPER_INSTRUCTIONS = (
    "Act as OSIA's provider. Use any applicable hosted or OSIA capability and "
    "report only observed outcomes. Apply the current OSIA application context "
    "when present for this turn's instructions and settings."
)

_PLANNING_BASE_INSTRUCTIONS = (
    "You are OSIA's planning-only structured-output module. Return exactly the "
    "single JSON value required by the current output schema. Never call, "
    "select, describe, or simulate tools or actions."
)

_PLANNING_DEVELOPER_INSTRUCTIONS = (
    "Planning-only turn: use no hosted, native, dynamic, command, file, web, "
    "desktop, or OSIA tools. Produce only the schema-constrained final JSON."
)


_DIRECT_VISUAL_TOOL_NAMES = frozenset({
    "request_visual_observation", "desktop_click",
    "computer_screenshot", "computer_zoom", "computer_observation",
    "computer_click", "computer_double_click", "computer_type",
    "computer_keypress", "computer_scroll", "computer_actions",
})


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
        self.public_reasoning_summary = str(
            getattr(error, "public_reasoning_summary", "") or ""
        )
        self.native_turn_id = getattr(error, "native_turn_id", None)
        self.response_items = tuple(
            dict(item)
            for item in (getattr(error, "response_items", ()) or ())
            if isinstance(item, dict)
        )
        self.final_output_item_id = str(
            getattr(error, "final_output_item_id", "") or ""
        )
        self.selected_model = str(selected_model) if selected_model else None
        self.reasoning_level = str(reasoning_level or "auto")


@dataclass
class _ConversationState:
    thread_id: str
    generation: int
    model: str
    system_fingerprint: str
    tool_schema_fingerprint: str
    runtime_client: AppServerClient
    expected_messages: list[dict]
    account_generation: int = 0
    planning_only: bool = False
    native_tool_names: tuple[str, ...] = ()
    strategy_revision: int = 0
    world_revision: int = 0
    account_scope: str = ""
    history_cursor: tuple[str, ...] = ()
    last_turn_id: str = ""
    application_context: str = ""
    context_policy: tuple[int, int] = ()


def _with_native_frontier_receipt(
    result,
    state: _ConversationState,
):
    values = {
        "native_tool_names": tuple(state.native_tool_names),
        "native_tool_schema_fingerprint": state.tool_schema_fingerprint,
        "strategy_revision": int(state.strategy_revision),
        "world_revision": int(state.world_revision),
    }
    if isinstance(result, TurnResult):
        return replace(result, **values)
    for key, value in values.items():
        try:
            setattr(result, key, value)
        except Exception:
            pass
    return result


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


def _message_digest(message: dict) -> str:
    payload = _public_message(message)
    if isinstance(payload["content"], str):
        payload["content"] = payload["content"].rstrip()
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _thread_delta(state: _ConversationState, messages: list[dict], turn_id: str) -> list[dict]:
    """Append unseen input to the same thread across history projection changes."""
    from osia.conversation_context import _is_tool_result

    if state.expected_messages and _same_messages(state.expected_messages, messages):
        return messages[len(state.expected_messages):]
    latest_user = next((
        i for i in range(len(messages) - 1, -1, -1)
        if messages[i].get("role") == "user" and not _is_tool_result(messages[i])
    ), None)
    same_turn = bool(turn_id and state.last_turn_id == turn_id)
    if state.history_cursor:
        digests = [_message_digest(message) for message in messages]
        limit = len(messages) if same_turn or latest_user is None else latest_user
        for count in range(min(len(state.history_cursor), limit), 0, -1):
            anchor = state.history_cursor[-count:]
            for end in range(limit, count - 1, -1):
                if tuple(digests[end-count:end]) == anchor:
                    return messages[end:]
    if latest_user is not None:
        return messages[latest_user + int(same_turn):]
    return messages[-1:]


def _native_rebuild_history(messages: list[dict]) -> list[dict]:
    """Replay the canonical visual context selected by the Chat runtime."""
    return [dict(message) for message in messages if message.get("role") != "system"]


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


def _append_instructions(text: str, addition: str) -> str:
    """Append provider context without adding any response-language preference."""
    return (str(text or "").rstrip() + "\n\n" + str(addition or "").strip()).strip()


def _sanitized_system_prompt(system: str, messages: list[dict]) -> str:
    """Remove eager local paths; re-add Desktop only when this request needs it."""
    desktop_match = re.search(r"(?m)^\[DESKTOP PATH: (.+?)\]\s*$", system or "")
    desktop_path = desktop_match.group(1) if desktop_match else None
    cleaned = re.sub(r"(?m)^\[DESKTOP PATH: .+?\]\s*$", "", system or "")
    cleaned = re.sub(r"(?m)^\[PROJECT ROOT: .+?\]\s*$", "", cleaned)
    cleaned = re.sub(
        r"(?m)^Your working directory is .+?\.?\s*$",
        "",
        cleaned,
    )
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
        cleaned = _append_instructions(
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
    def __init__(
        self,
        client: AppServerClient | None = None,
        *,
        direct_visual_client: AppServerClient | None = None,
    ):
        self.client = client or AppServerClient()
        self._direct_visual_client = (
            direct_visual_client
            if direct_visual_client is not None else
            self.client
            if client is not None else
            AppServerClient(
                base_dir=self.client.base_dir,
                disable_code_mode_host=True,
                runtime_partition="visual-direct",
                compact_logs_on_startup=False,
            )
        )
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._models: list[dict] = []
        self._models_account_generation = getattr(self.client, "account_generation", 0)
        self._threads: dict[str, _ConversationState] = {}
        self._conversation_locks: dict[str, threading.RLock] = {}
        self._account_scope_cache: tuple[int, int, str] | None = None

    def _conversation_lock(self, conversation_id: str) -> threading.RLock:
        identity = str(conversation_id or "")
        with self._lock:
            return self._conversation_locks.setdefault(
                identity,
                threading.RLock(),
            )

    def status(self, *, refresh_models: bool = True) -> dict:
        try:
            health = self.client.health_check()
            account = self.client.get_account_status()
            if account["connected"]:
                models = self.list_models(force=True) if refresh_models else self.cached_models()
                selected = self.selected_model(models)
            else:
                with self._lock:
                    self._models.clear()
                    self._threads.clear()
                models = []
                selected = None
            feature_enabled = bool(get_setting("chatgpt_account_feature_enabled", True))
            return {
                "available": bool(feature_enabled and account["connected"] and models and selected),
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
            cached = self.cached_models()
            if cached and not force:
                return cached
        account_generation = getattr(self.client, "account_generation", 0)
        models = self.client.list_models()
        with self._lock:
            if account_generation != getattr(self.client, "account_generation", 0):
                self._models.clear()
                return []
            self._models = [dict(model) for model in models]
            self._models_account_generation = account_generation
            return [dict(model) for model in self._models]

    def cached_models(self) -> list[dict]:
        with self._lock:
            if self._models_account_generation != getattr(self.client, "account_generation", 0):
                self._models.clear()
            return [dict(model) for model in self._models]

    def selected_model(self, models: list[dict] | None = None) -> str | None:
        models = models if models is not None else self.list_models()
        selected = str(get_setting("chatgpt_account_model", "") or "").strip()
        # Catalog visibility is not entitlement. Preserve an explicit model
        # even if discovery changes; only its actual request can prove access
        # or return an actionable provider error. Never select another model.
        if selected:
            return selected
        default = next((m for m in models if m.get("isDefault")), None)
        chosen = default or (models[0] if models else None)
        return str(chosen.get("id")) if chosen else None

    def set_selected_model(self, model_id: str) -> None:
        valid = {str(model.get("id")) for model in self.list_models() if model.get("id")}
        if model_id not in valid:
            raise AppServerError("ERROR: that ChatGPT account model is unavailable.", category="model_unavailable")
        set_setting("chatgpt_account_model", model_id)

    def begin_login(self) -> dict:
        return self.client.begin_login()

    def begin_device_code_login(self) -> dict:
        return self.client.begin_device_code_login()

    def wait_for_login(self, login_id: str, timeout: float = 300.0) -> dict:
        status = self.client.wait_for_login(login_id, timeout=timeout)
        if status.get("connected"):
            self.reset_login()
            self.list_models(force=True)
        notify_account_changed()
        return status

    def cancel_login(self) -> None:
        self.client.cancel_login()
        notify_account_changed()

    def reset_login(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                states = list(self._threads.values())
            for state in states:
                state.runtime_client.cancel_turn(thread_id=state.thread_id)
            if self._direct_visual_client is not self.client:
                self._direct_visual_client.shutdown()
            self.client.reset_login()
            with self._lock:
                self._threads.clear()
                self._account_scope_cache = None

    def logout(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                states = list(self._threads.values())
            for state in states:
                state.runtime_client.cancel_turn(thread_id=state.thread_id)
            if self._direct_visual_client is not self.client:
                self._direct_visual_client.shutdown()
            self.client.logout()
            with self._lock:
                self._models.clear()
                self._threads.clear()
                self._account_scope_cache = None
        notify_account_changed()

    def rate_limits(self) -> dict:
        return self.client.get_rate_limits()

    def chat_client(self, conversation_id: str | None = None):
        return _AccountChatClient(self, conversation_id=conversation_id)

    def _account_scope(self, client) -> str:
        key = (client.generation, getattr(client, "account_generation", 0))
        cached = self._account_scope_cache
        if cached and cached[:2] == key:
            return cached[2]
        read_account = getattr(client, "get_account_status", None)
        account = (read_account().get("account") or {}) if callable(read_account) else {}
        email = str(account.get("email") or "").strip().casefold()
        scope = hashlib.sha256(email.encode()).hexdigest() if email else ""
        self._account_scope_cache = (*key, scope)
        return scope

    @staticmethod
    def _store_thread_state(
        conversation_id: str, state: _ConversationState, previous_thread_id: str,
        *, capture_usage: bool = False,
    ) -> bool:
        from osia.conversations import store_chatgpt_thread

        binding = {
            "thread_id": state.thread_id,
            "account_scope": state.account_scope,
            "history_cursor": list(state.history_cursor),
            "last_turn_id": state.last_turn_id,
            "system_fingerprint": state.system_fingerprint,
            "tool_schema_fingerprint": state.tool_schema_fingerprint,
            "native_tool_names": list(state.native_tool_names),
            "model": state.model,
        }
        read_usage = getattr(state.runtime_client, "thread_context_usage", None)
        if capture_usage and callable(read_usage):
            usage = read_usage(state.thread_id)
            from osia.conversation_context import _context_usage_snapshot

            binding["context_usage"] = _context_usage_snapshot({
                **(usage or {}), "provider": ACCOUNT_PROVIDER_ID, "model": state.model,
            })
        return store_chatgpt_thread(
            conversation_id, binding, previous_thread_id=previous_thread_id,
        )

    @staticmethod
    def _thread_key(conversation_id: str, planning_only: bool) -> str:
        return "planning:" + conversation_id if planning_only else conversation_id

    def _prepare(
        self, conversation_id: str, model: str, messages: list[dict],
        *, planning_only: bool = False, osia_turn_id: str = "",
    ) -> tuple[_ConversationState, list[dict]]:
        public = [_public_message(message) for message in messages]
        system = next((m["content"] for m in public if m["role"] == "system"), "")
        system = _sanitized_system_prompt(system, public)
        if planning_only:
            base_instructions = _PLANNING_BASE_INSTRUCTIONS
            developer_instructions = _PLANNING_DEVELOPER_INSTRUCTIONS
        else:
            base_instructions = _append_instructions(
                system, _CONTAINMENT_INSTRUCTIONS,
            )
            developer_instructions = _DEVELOPER_INSTRUCTIONS
        if planning_only:
            dynamic_tools = ()
            strategy = None
        else:
            try:
                from osia.execution_strategy import current_execution_strategy

                strategy = current_execution_strategy()
                dynamic_tools = tuple(_canonical_native_tools(strategy))
            except Exception:
                dynamic_tools = ()
                strategy = None
        dynamic_tool_names = tuple(
            str(item.get("name") or "")
            for item in dynamic_tools
            if isinstance(item, dict)
        )
        direct_visual_turn = bool(dynamic_tool_names) and all(
            name in _DIRECT_VISUAL_TOOL_NAMES for name in dynamic_tool_names
        )
        runtime_client = (
            self._direct_visual_client if direct_visual_turn else self.client
        )
        from osia.conversations import get_conversation

        conv = (
            get_conversation(conversation_id)
            if not planning_only and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id)
            else None
        )
        if conv is not None:
            runtime_client = self.client
        # Establish process ownership before deciding whether an existing
        # thread is still loaded. run_turn() may otherwise restart the child
        # only after we have chosen a stale thread from the previous process.
        start_runtime = getattr(runtime_client, "start", None)
        refresh_account_storage = getattr(runtime_client, "refresh_account_storage", None)
        if callable(refresh_account_storage):
            refresh_account_storage()
        if callable(start_runtime):
            start_runtime()
        read_capacity = getattr(runtime_client, "model_context_capacity", None)
        capacity = read_capacity(model) if callable(read_capacity) else None
        context_policy = (
            (capacity["context_window"], capacity["auto_compact_token_limit"])
            if capacity else ()
        )
        account_generation = getattr(runtime_client, "account_generation", 0)
        scope = self._account_scope(runtime_client) if conv is not None else ""
        stored = (conv or {}).get("_chatgpt_account_thread") or {}
        if not isinstance(stored, dict):
            stored = {}
        stored_id = str(stored.get("thread_id") or "")
        saved = bool(scope and stored_id and stored.get("account_scope") == scope)
        frontier_names = [
            str(item.get("name") or "")
            for item in dynamic_tools
            if isinstance(item, dict)
        ]
        tool_schema_fingerprint = hashlib.sha256(
            json.dumps(
                dynamic_tools,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        strategy_snapshot = (
            strategy.snapshot() if strategy is not None else {}
        )
        try:
            strategy_revision = max(
                0, int(strategy_snapshot.get("evidence_revision") or 0),
            )
        except (TypeError, ValueError, OverflowError):
            strategy_revision = 0
        latest_observation = strategy_snapshot.get(
            "latest_desktop_observation"
        )
        latest_observation = (
            latest_observation
            if isinstance(latest_observation, dict) else {}
        )
        try:
            world_revision = max(
                strategy_revision,
                int(latest_observation.get("evidence_revision") or 0),
            )
        except (TypeError, ValueError, OverflowError):
            world_revision = strategy_revision
        fingerprint_input = (
            f"{int(planning_only)}\0{system}\0"
            + json.dumps(frontier_names, separators=(",", ":"))
            + "\0"
            + tool_schema_fingerprint
        )
        system_fingerprint = hashlib.sha256(
            fingerprint_input.encode("utf-8"),
        ).hexdigest()
        non_system = [message for message in messages if message.get("role") != "system"]
        key = self._thread_key(conversation_id, planning_only)
        with self._lock:
            state = self._threads.get(key)
            reusable = bool(
                state
                and state.runtime_client is runtime_client
                and state.generation == runtime_client.generation
                and state.account_generation == account_generation
                and state.account_scope == scope
                and state.context_policy == context_policy
                and (not scope or (saved and state.thread_id == stored_id))
                and (not saved or state.history_cursor == tuple(stored.get("history_cursor") or ()))
            )
            if planning_only and reusable:
                reusable = state.system_fingerprint == system_fingerprint and state.model == model
        if not reusable:
            parameters = {
                "model": model, "base_instructions": base_instructions,
                "developer_instructions": developer_instructions,
                "dynamic_tools": dynamic_tools,
                **({"persistent": True} if scope else {}),
            }
            try:
                thread_id = runtime_client.start_or_resume_conversation(
                    thread_id=stored_id if saved else None, **parameters,
                )
            except AppServerError as exc:
                if not saved or exc.category != "thread_missing":
                    raise
                # An empty or deleted rollout has nothing to resume. Rebuild
                # from OSIA's canonical history only after that exact diagnosis.
                saved = False
                thread_id = runtime_client.start_or_resume_conversation(thread_id=None, **parameters)
            state = _ConversationState(
                thread_id=thread_id,
                generation=runtime_client.generation,
                account_generation=account_generation,
                model=model,
                system_fingerprint=str(stored.get("system_fingerprint") or "") if saved else system_fingerprint,
                tool_schema_fingerprint=str(stored.get("tool_schema_fingerprint") or "") if saved else tool_schema_fingerprint,
                runtime_client=runtime_client,
                expected_messages=[],
                planning_only=planning_only,
                native_tool_names=tuple(stored.get("native_tool_names") or ()) if saved else tuple(frontier_names),
                strategy_revision=strategy_revision,
                world_revision=world_revision,
                account_scope=scope,
                history_cursor=tuple(stored.get("history_cursor") or ()) if saved else (),
                last_turn_id=str(stored.get("last_turn_id") or "") if saved else "",
                context_policy=context_policy,
            )
            if scope and not saved and not self._store_thread_state(conversation_id, state, stored_id):
                raise AppServerError(
                    "ERROR: OSIA could not bind the ChatGPT thread to this conversation.",
                    allow_fallback=False, category="thread_binding_failed",
                )
            with self._lock:
                self._threads[key] = state
        state.model = model
        state.strategy_revision = strategy_revision
        state.world_revision = world_revision
        state.application_context = (
            _append_instructions(
                base_instructions,
                "Use native calls only for tools declared on this thread. "
                "Use OSIA's textual format for other available tools.",
            )
            if state.system_fingerprint != system_fingerprint and not planning_only else ""
        )
        delta = (
            _thread_delta(state, non_system, osia_turn_id)
            if reusable or (saved and state.history_cursor) else
            _native_rebuild_history(non_system)
        )
        if not delta and osia_turn_id and state.last_turn_id == osia_turn_id:
            delta = [{
                "role": "user",
                "content": (
                    "Continue the current OSIA task using the observations already in this thread. "
                    "Keep the completed steps and continue with the next unfinished step."
                ),
            }]
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
        on_reasoning_summary=None,
        timeout: float = 300.0,
        output_schema: dict | None = None,
        planning_only: bool = False,
    ) -> TurnResult:
        with self._conversation_lock(conversation_id):
            return self._complete_serialized(
                conversation_id=conversation_id,
                model=model,
                messages=messages,
                osia_turn_id=osia_turn_id,
                client_request_id=client_request_id,
                final_response_message_id=final_response_message_id,
                effort=effort,
                on_delta=on_delta,
                on_reasoning_summary=on_reasoning_summary,
                timeout=timeout,
                output_schema=output_schema,
                planning_only=planning_only,
            )

    def _complete_serialized(
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
        on_reasoning_summary=None,
        timeout: float = 300.0,
        output_schema: dict | None = None,
        planning_only: bool = False,
    ) -> TurnResult:
        state = None
        lifecycle_gate_held = False
        key = self._thread_key(conversation_id, planning_only)

        def release_lifecycle_gate() -> None:
            nonlocal lifecycle_gate_held
            if lifecycle_gate_held:
                self._lifecycle_lock.release()
                lifecycle_gate_held = False

        def remember_input(assistant_text: str | None = None) -> None:
            if state is None:
                return
            public = [
                _public_message(message) for message in messages
                if message.get("role") != "system"
            ]
            if assistant_text is not None:
                public.append({"role": "assistant", "content": assistant_text})
            with self._lock:
                if self._threads.get(key) is not state:
                    return
                state.expected_messages = public
                state.history_cursor = tuple(_message_digest(item) for item in public[-24:])
                state.last_turn_id = str(osia_turn_id or "")
            if state.account_scope:
                try:
                    self._store_thread_state(
                        conversation_id, state, state.thread_id,
                        capture_usage=assistant_text is not None,
                    )
                except OSError:
                    # The already accepted provider turn remains authoritative.
                    pass

        def registered() -> None:
            try:
                remember_input()
            finally:
                release_lifecycle_gate()

        try:
            self._lifecycle_lock.acquire()
            lifecycle_gate_held = True
            state, delta_messages = self._prepare(
                conversation_id, model, messages,
                planning_only=planning_only,
                osia_turn_id=str(osia_turn_id or ""),
            )
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
                on_reasoning_summary=on_reasoning_summary,
                timeout=_bounded_turn_timeout(timeout),
                output_schema=output_schema,
                on_attempt_registered=registered,
            )
            if state.application_context:
                run_kwargs["application_context"] = state.application_context
            if isinstance(turn_input, list):
                run_kwargs["content"] = turn_input
            else:
                run_kwargs["text"] = turn_input
            try:
                result = state.runtime_client.run_turn(**run_kwargs)
                result = _with_native_frontier_receipt(result, state)
            finally:
                release_lifecycle_gate()
            remember_input(result.text)
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
                    public_reasoning_summary=str(
                        getattr(exc, "public_reasoning_summary", "") or ""
                    ),
                    native_tool_names=tuple(state.native_tool_names),
                    native_tool_schema_fingerprint=(
                        state.tool_schema_fingerprint
                    ),
                    strategy_revision=int(state.strategy_revision),
                    world_revision=int(state.world_revision),
                    native_tool_call=(
                        dict(exc.native_tool_call)
                        if isinstance(exc.native_tool_call, dict) else None
                    ),
                )
                if on_reasoning_summary is not None and result.public_reasoning_summary:
                    on_reasoning_summary(result.public_reasoning_summary)
                remember_input(result.text)
                return result
            if exc.native_action and state is not None:
                remember_input()
            raise AccountProviderError(
                exc,
                selected_model=model,
                reasoning_level=effort,
            ) from exc
        finally:
            release_lifecycle_gate()

    def cancel_conversation(self, conversation_id: str) -> None:
        with self._lock:
            state = self._threads.get(conversation_id)
        if state:
            state.runtime_client.cancel_turn(thread_id=state.thread_id)

    def conversation_context_usage(
        self, conversation_id: str, *, model: str = "", conversation: dict | None = None,
    ) -> dict | None:
        if conversation is not None and (not isinstance(conversation, dict) or str(conversation.get("id") or "") != conversation_id):
            return None
        with self._lock:
            state = self._threads.get(conversation_id)
        if state is not None and state.account_generation != getattr(state.runtime_client, "account_generation", 0):
            return None
        if state is not None and not (model and state.model != model):
            read_usage = getattr(state.runtime_client, "thread_context_usage", None)
            usage = read_usage(state.thread_id) if callable(read_usage) else None
            if usage:
                return usage
        from osia import conversations
        from osia.conversation_context import _context_usage_snapshot

        conv = conversation if conversation is not None else conversations.get_conversation(conversation_id) or {}
        if not isinstance(conv, dict):
            return None
        binding = conv.get("_chatgpt_account_thread") or {}
        if not isinstance(binding, dict) or not binding.get("thread_id"):
            return None
        client = state.runtime_client if state is not None else self.client
        read_saved = getattr(client, "saved_thread_context_usage", None)
        native = read_saved(binding["thread_id"]) if callable(read_saved) else None
        candidate = native if native is not None else binding.get("context_usage")
        if not isinstance(candidate, dict):
            return None
        measured_model = str(candidate.get("model") or binding.get("model") or "")
        if model and measured_model and model != measured_model:
            return None
        usage = _context_usage_snapshot({
            **candidate, "provider": ACCOUNT_PROVIDER_ID, "model": measured_model,
        })
        if not usage:
            return None
        return {
            "used": usage["used"], "maximum": usage["maximum"], "estimated": usage["estimated"],
            "compacting": False, "restored": True,
        }

    def compact_conversation(self, conversation_id: str, model: str) -> bool:
        """Compact the same saved ChatGPT thread without adding a chat message."""
        from osia.conversations import get_conversation

        with self._conversation_lock(conversation_id), self._lifecycle_lock:
            conv = get_conversation(conversation_id) or {}
            binding = conv.get("_chatgpt_account_thread") or {}
            thread_id = str(binding.get("thread_id") or "")
            if not thread_id:
                return False
            client = self.client
            client.start()
            if not binding.get("account_scope") or binding["account_scope"] != self._account_scope(client):
                return False
            # Resume the exact thread even when already loaded: the user may
            # have changed the selected model/window without sending a turn.
            try:
                client.start_or_resume_conversation(
                    thread_id=thread_id, model=model, base_instructions="",
                    developer_instructions="", persistent=True,
                )
            except AppServerError as exc:
                if exc.category == "thread_missing":
                    return False
                raise
            client.compact_thread(thread_id)
            read_usage = getattr(client, "thread_context_usage", None)
            from osia.conversation_context import _context_usage_snapshot

            usage = read_usage(thread_id) if callable(read_usage) else None
            conversations_binding = {
                **binding, "model": model,
                "context_usage": _context_usage_snapshot({
                    **(usage or {}), "provider": ACCOUNT_PROVIDER_ID, "model": model,
                }),
            }
            from osia.conversations import store_chatgpt_thread

            store_chatgpt_thread(
                conversation_id, conversations_binding, previous_thread_id=thread_id,
            )
            return True

    def release_ephemeral_conversation(self, conversation_id: str) -> None:
        """Forget local state for a completed one-shot observation turn."""

        with self._lock:
            self._threads.pop(str(conversation_id or ""), None)

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._direct_visual_client is not self.client:
                self._direct_visual_client.shutdown()
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
        output_schema = kwargs.get("output_schema")
        planning_only = kwargs.get("planning_only") is True
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
                    on_reasoning_summary=(
                        None
                        if planning_only else
                        self._owner._emit_public_reasoning_summary
                    ),
                    timeout=timeout,
                    output_schema=output_schema,
                    planning_only=planning_only,
                )
            except Exception as exc:
                public_summary = str(
                    getattr(exc, "public_reasoning_summary", "") or ""
                )
                if public_summary and not planning_only:
                    self._owner._emit_public_reasoning_summary(public_summary)
                self._owner._preserve_exception_state(exc)
                raise
            self._owner._set_capability_events(
                getattr(result, "capability_events", ()),
            )
            self._owner._set_native_tool_handoff(
                getattr(result, "native_tool_call", None),
            )
            self._owner._set_invocation_metadata({
                "native_turn_id": getattr(result, "turn_id", None),
                "native_thread_id": getattr(result, "thread_id", None),
                "native_status": getattr(result, "status", ""),
                "output_started": bool(getattr(result, "output_started", False)),
                "response_items": tuple(
                    dict(item)
                    for item in (getattr(result, "response_items", ()) or ())
                    if isinstance(item, dict)
                ),
                "final_output_item_id": str(
                    getattr(result, "final_output_item_id", "") or ""
                ),
                "model_rerouted": bool(
                    getattr(result, "model_rerouted", False)
                ),
                "effective_model_id": str(
                    getattr(result, "effective_model_id", "") or model
                ),
                "native_tool_names": tuple(
                    getattr(result, "native_tool_names", ()) or ()
                ),
                "native_tool_schema_fingerprint": str(
                    getattr(result, "native_tool_schema_fingerprint", "") or ""
                ),
                "strategy_revision": int(
                    getattr(result, "strategy_revision", 0) or 0
                ),
                "world_revision": int(
                    getattr(result, "world_revision", 0) or 0
                ),
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
                    on_reasoning_summary=(
                        None
                        if planning_only else
                        lambda summary: events.put(
                            ("public_reasoning_summary", summary)
                        )
                    ),
                    timeout=timeout,
                    output_schema=output_schema,
                    planning_only=planning_only,
                )
                self._owner._set_capability_events(
                    getattr(result, "capability_events", ()),
                )
                self._owner._set_native_tool_handoff(
                    getattr(result, "native_tool_call", None),
                )
                self._owner._set_invocation_metadata({
                    "native_turn_id": getattr(result, "turn_id", None),
                    "native_thread_id": getattr(result, "thread_id", None),
                    "native_status": getattr(result, "status", ""),
                    "output_started": bool(getattr(result, "output_started", False)),
                    "response_items": tuple(
                        dict(item)
                        for item in (getattr(result, "response_items", ()) or ())
                        if isinstance(item, dict)
                    ),
                    "final_output_item_id": str(
                        getattr(result, "final_output_item_id", "") or ""
                    ),
                    "model_rerouted": bool(
                        getattr(result, "model_rerouted", False)
                    ),
                    "effective_model_id": str(
                        getattr(result, "effective_model_id", "") or model
                    ),
                    "native_tool_names": tuple(
                        getattr(result, "native_tool_names", ()) or ()
                    ),
                    "native_tool_schema_fingerprint": str(
                        getattr(
                            result, "native_tool_schema_fingerprint", "",
                        ) or ""
                    ),
                    "strategy_revision": int(
                        getattr(result, "strategy_revision", 0) or 0
                    ),
                    "world_revision": int(
                        getattr(result, "world_revision", 0) or 0
                    ),
                })
                events.put(("done", None))
            except Exception as exc:
                public_summary = str(
                    getattr(exc, "public_reasoning_summary", "") or ""
                )
                if public_summary and not planning_only:
                    events.put(("public_reasoning_summary", public_summary))
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
                elif kind == "public_reasoning_summary":
                    self._owner._emit_public_reasoning_summary(payload)
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
        self._public_reasoning_summary_callback = None
        self._public_reasoning_summary_lock = threading.Lock()
        self._native_tool_handoff: dict = {}
        self._native_tool_handoff_lock = threading.Lock()
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
            "response_items": tuple(
                dict(item)
                for item in (getattr(exc, "response_items", ()) or ())
                if isinstance(item, dict)
            ),
            "final_output_item_id": str(
                getattr(exc, "final_output_item_id", "") or ""
            ),
        })

    def consume_capability_events(self) -> tuple:
        """Return provider-native events from exactly one completed model call."""

        with self._capability_events_lock:
            events = self._capability_events
            self._capability_events = ()
        return events

    def _set_native_tool_handoff(self, value) -> None:
        with self._native_tool_handoff_lock:
            self._native_tool_handoff = (
                dict(value) if isinstance(value, Mapping) else {}
            )

    def consume_native_tool_handoff(self) -> dict:
        """Return one exact out-of-band native call correlation mapping."""

        with self._native_tool_handoff_lock:
            value = dict(self._native_tool_handoff)
            self._native_tool_handoff = {}
        return value

    def _set_invocation_metadata(self, metadata) -> None:
        with self._invocation_metadata_lock:
            self._invocation_metadata = dict(metadata or {})

    def consume_invocation_metadata(self) -> dict:
        """Return native identity/status for one completed provider segment."""

        with self._invocation_metadata_lock:
            metadata = dict(self._invocation_metadata)
            self._invocation_metadata = {}
        return metadata

    def set_public_reasoning_summary_callback(self, callback) -> None:
        """Bind the active Chat trace consumer; planning calls never invoke it."""

        with self._public_reasoning_summary_lock:
            self._public_reasoning_summary_callback = (
                callback if callable(callback) else None
            )

    def _emit_public_reasoning_summary(self, payload) -> None:
        with self._public_reasoning_summary_lock:
            callback = self._public_reasoning_summary_callback
        if callable(callback) and isinstance(payload, Mapping):
            callback(dict(payload))

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


def notify_account_changed() -> None:
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


def shutdown_account_provider() -> None:
    """Close an existing account runtime without creating one during exit."""
    with _PROVIDER_LOCK:
        provider = _PROVIDER
    if provider is not None:
        provider.shutdown()


def delete_saved_chatgpt_threads(bindings: list[tuple[str, str]]) -> None:
    """Clean up only the native threads of canonical, committed deletions."""
    exact = {
        (conv_id, thread_id) for conv_id, thread_id in bindings
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conv_id)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id)
    }
    if not exact:
        return

    def delete() -> None:
        provider = get_account_provider()
        for conv_id, thread_id in exact:
            with provider._conversation_lock(conv_id):
                with provider._lock:
                    state = provider._threads.get(conv_id)
                    if state is not None and state.thread_id == thread_id:
                        provider._threads.pop(conv_id, None)
                try:
                    provider.client.delete_thread(thread_id)
                except (OSError, AppServerError):
                    print("[OSIA] saved ChatGPT thread cleanup could not be confirmed")

    threading.Thread(target=delete, name="osia-delete-chatgpt-threads", daemon=True).start()
