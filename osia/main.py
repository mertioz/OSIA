#!/usr/bin/env python3
import os
import sys
from osia.utils.diagnostics import diagnostic_print as print
# The Qt GUI must run under the X11 (xcb) platform plugin on Linux. On Windows
# Qt uses its native "windows" plugin automatically, so xcb must NOT be forced
# (it would fail to start). Guard it to Linux only â€” WOR-11 (Windows port).
if sys.platform.startswith("linux"):
    os.environ["QT_QPA_PLATFORM"] = "xcb"
# On Windows, the console's default encoding (cp1252) raises UnicodeEncodeError
# when printing emoji / non-ASCII (e.g. the "â–¶" in "[OSIA Agent] â–¶ tool:").
# Reconfigure stdout/stderr to UTF-8 (replace on error) before any code prints
# non-ASCII â€” WOR-11. Guarded for the detached-launch case (autostart under
# pythonw), where stdout/stderr may be None or lack .reconfigure â€” this must
# never crash startup.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None and hasattr(_stream, "reconfigure"):
                _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# WOR-46 (Windows UI Automation click backend): pin the process DPI awareness
# ONCE, here at startup â€” BEFORE Qt's QApplication is ever constructed and BEFORE
# the `uiautomation` library can be imported (it sets DPI awareness itself at
# import time). DPI awareness can only be set once per process, so fixing it
# FIRST means Qt (which would otherwise enable Per-Monitor-v2 on its own) and
# uiautomation both inherit THIS mode instead of racing to set divergent ones.
# The payoff: UIA BoundingRectangle, Pillow ImageGrab (vision capture) and
# pyautogui clicks then all share ONE physical-pixel coordinate space, so a rect
# center resolved by UIA lands exactly where the click is sent. We request
# Per-Monitor-Aware-v2, which is already Qt6's default â€” so the GUI renders
# identically (no regression) and stays coherent with the PassThrough rounding
# policy wake_mode() sets before its QApplication. Best-effort: every call is
# wrapped in try/except so a missing API on older Windows never crashes startup.
if sys.platform == "win32":
    try:
        import ctypes
        try:
            # Windows 10 1703+: modern per-monitor-v2 context (== Qt6's default).
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            )
        except Exception:
            # Windows 8.1+ fallback: 2 == PROCESS_PER_MONITOR_DPI_AWARE.
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
"""
OSIA main entry point â€” supports text mode, voice mode, wake-word mode.

Run:
  python osia/main.py              # Wake word mode with wake screen (default)
  python osia/main.py --text       # Text mode
  python osia/main.py --voice      # Voice mode: speak command, get spoken response
  python osia/main.py --wake       # Wake word mode with wake screen
"""

# Silence the noisy startup chatter from torch / transformers / kokoro.
# Must run before any module that transitively imports them â€” otherwise the
# import-time warnings have already been emitted.
import warnings
warnings.filterwarnings("ignore", module=r"torch.*")
warnings.filterwarnings("ignore", module=r"transformers.*")
warnings.filterwarnings("ignore", module=r"kokoro.*")

import sys
import os
import threading
import time
import re
import unicodedata
import uuid
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osia.agent import (
    OSIAAgent,
    DEFAULT_LANG,
    get_system_prompt,
    _sanitize_persisted_history_message,
)
from osia import conversations
from osia.tools import verify_tools

# Loopback trigger for the mobile WebSocket server (osia/server/server.py,
# listens on :8765). Used by the "nouveau chat" voice command (WOR-10) to push
# a {"type": "new_chat"} reset to every connected device. The endpoint is
# localhost-only on the server side.
_SERVER_NEW_CHAT_URL = "http://127.0.0.1:8765/internal/new_chat"
_SERVER_CONVERSATION_CHANGED_URL = (
    "http://127.0.0.1:8765/internal/conversation_changed"
)
_SERVER_MOBILE_CATALOGUE_URL = (
    "http://127.0.0.1:8765/internal/mobile_catalogue_changed"
)
_SERVER_CHATGPT_ACCOUNT_URL = (
    "http://127.0.0.1:8765/internal/chatgpt_account_changed"
)
LIVEAPI_AVAILABLE = False
# Desktopâ†’server MCP state mirror (Phase 1, direction A). The desktop pings this
# after a local MCP connect/disconnect so the mobile server reconciles its OWN
# manager and repaints every phone. Same loopback host as new_chat; carries only
# {id, action} (no secret); localhost-only on the server side.
_SERVER_MCP_SYNC_URL = "http://127.0.0.1:8765/internal/mcp_sync"
# WOR-96 PART 1 â€” desktop entry point for an OAuth connect that completes via the
# HTTPS tunnel callback (app.osia.space â†’ the SERVER process). The desktop kicks the
# server (which runs the OAuth provider so the tunnel redirect lands in the same
# process as the PKCE state) and opens the returned authorize URL in the PC
# browser. Separate from the existing loopback Connect button, which is unchanged.
_SERVER_MCP_OAUTH_TUNNEL_URL = "http://127.0.0.1:8765/internal/mcp_oauth_tunnel_connect"
# WOR-97 â€” desktop entry point to abort an in-flight OAuth login running in the
# SERVER process (tunnel/PART-1 flow). The desktop POSTs {id} to this loopback
# route, which calls MCPManager.cancel_pending server-side and rebroadcasts.
_SERVER_MCP_OAUTH_CANCEL_URL = "http://127.0.0.1:8765/internal/mcp_oauth_cancel"
# WOR-98 â€” desktop entry point to "switch account" on a CONNECTED OAuth connector.
# The desktop POSTs {id} to this loopback route, which tears the session down,
# REVOKES the grant (RFC 7009) AND forgets the DCR client (delete_mcp_oauth) so the
# next Connect re-runs DCR + a fresh login, then rebroadcasts the reset.
_SERVER_MCP_OAUTH_SWITCH_URL = "http://127.0.0.1:8765/internal/mcp_oauth_switch"
_SERVER_CRON_NOTIFICATION_URL = "http://127.0.0.1:8765/internal/cron_notification"
_MOBILE_TRACE_FORWARDER = None
_MOBILE_TRACE_FORWARDER_LOCK = threading.Lock()
_MOBILE_TRACE_ANNOUNCED = {}


def _notify_mobile_execution_trace(frame: dict):
    """Forward public Desktop events in order without blocking Qt/provider work."""
    from concurrent.futures import ThreadPoolExecutor
    from osia.execution_trace import validate_execution_trace_frame

    safe = validate_execution_trace_frame(frame)
    if safe is None:
        return None

    def worker():
        import json
        import urllib.request

        key = tuple(safe[name] for name in (
            "conversation_id", "request_id", "turn_id", "run_id",
        ))
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:8765/internal/execution_trace",
                data=json.dumps({
                    "frame": safe, "announce": key not in _MOBILE_TRACE_ANNOUNCED,
                }).encode("utf-8"),
                method="POST", headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=1.5) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("ok"):
                _MOBILE_TRACE_ANNOUNCED[key] = True
                while len(_MOBILE_TRACE_ANNOUNCED) > 64:
                    _MOBILE_TRACE_ANNOUNCED.pop(next(iter(_MOBILE_TRACE_ANNOUNCED)))
        except Exception:
            # Optional projection delivery never changes canonical execution.
            pass

    global _MOBILE_TRACE_FORWARDER
    with _MOBILE_TRACE_FORWARDER_LOCK:
        if _MOBILE_TRACE_FORWARDER is None:
            _MOBILE_TRACE_FORWARDER = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="osia-thinking-mirror",
            )
        return _MOBILE_TRACE_FORWARDER.submit(worker)


def _notify_mobile_new_chat() -> None:
    """Best-effort: ask the mobile server to broadcast a new-chat reset to all
    connected devices. Silent no-op when the server isn't running."""
    import urllib.request
    try:
        req = urllib.request.Request(
            _SERVER_NEW_CHAT_URL, data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1.5).close()
    except Exception as e:
        print(f"[WOR-10] mobile new_chat notify skipped: {e}")


def _notify_mobile_state_changed(url: str, label: str) -> None:
    """Best-effort non-blocking loopback signal carrying no state or secret."""
    import urllib.request

    def worker() -> None:
        try:
            req = urllib.request.Request(
                url,
                data=b"{}",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=1.5).close()
        except Exception as e:
            print(f"[OSIA] mobile {label} notify skipped: {e}")

    threading.Thread(
        target=worker,
        name=f"osia-mobile-{label}-notify",
        daemon=True,
    ).start()


def _deletion_fallback_ids(
    items: list[dict], selected_id: str, excluded_ids: set[str],
) -> list[str]:
    """Prefer the next visible row, then earlier rows in reverse order."""
    index = next(
        (pos for pos, item in enumerate(items)
         if str(item.get("id") or "") == selected_id),
        len(items),
    )
    ordered = items[index + 1:] + list(reversed(items[:index]))
    return [
        conv_id for item in ordered
        if (conv_id := str(item.get("id") or ""))
        and conv_id not in excluded_ids
    ]


def _notify_mobile_conversation_changed(
    conv_id: str,
    *,
    revision: int = 0,
    operation_id: str = "",
    action: str = "upsert",
    deleted_ids: list[str] | None = None,
) -> None:
    """Best-effort canonical-store invalidation; carries no chat content."""

    import json
    import urllib.request

    payload = json.dumps({
        "conversation_id": str(conv_id or ""),
        "revision": max(0, int(revision or 0)),
        "operation_id": str(operation_id or ""),
        "action": str(action or "upsert"),
        "deleted_ids": [str(item or "") for item in (deleted_ids or [])],
    }).encode("utf-8")

    def worker() -> None:
        try:
            request = urllib.request.Request(
                _SERVER_CONVERSATION_CHANGED_URL,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=1.5) as response:
                try:
                    result = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    result = {}
                print(
                    "[OSIA DeleteTrace] origin=PC phase=notification_result "
                    f"conversation_id={str(conv_id or '')!r} "
                    f"operation_id={str(operation_id or '')!r} "
                    f"action={str(action or 'upsert')} "
                    f"revision={max(0, int(revision or 0))} "
                    f"broadcast={bool(result.get('broadcast'))} "
                    f"http_status={getattr(response, 'status', 0)}",
                    flush=True,
                )
        except Exception as exc:
            print(
                "[OSIA] mobile conversation notify skipped: "
                f"{type(exc).__name__}"
            )

    threading.Thread(
        target=worker,
        name="osia-mobile-conversation-notify",
        daemon=True,
    ).start()


def _resolve_desktop_chat_route_binding(turn_directives=None, *, task_text="") -> dict[str, str]:
    from osia.agent import MODEL as DEFAULT_MODEL
    from osia.config import load_settings
    from osia.task_router import resolve_chat_route_binding

    return resolve_chat_route_binding(
        load_settings(), DEFAULT_MODEL, turn_directives, task_text=task_text,
    )


def _new_desktop_turn_meta(
    conv_id: str,
    *,
    turn_directives=None,
    task_text: str = "",
) -> dict:
    request_id = f"desktop_req_{uuid.uuid4().hex}"
    turn_id = f"desktop_turn_{uuid.uuid4().hex}"
    route = _resolve_desktop_chat_route_binding(turn_directives, task_text=task_text)
    from osia.turn_terminality import live_process_identity

    _owner_alive, owner_process_identity = live_process_identity(os.getpid())
    return {
        "conversation_id": conv_id,
        "request_id": request_id,
        "client_request_id": request_id,
        "turn_id": turn_id,
        "run_id": turn_id,
        **route,
        "user_message_id": f"desktop_msg_{uuid.uuid4().hex}",
        "assistant_message_id": f"desktop_msg_{uuid.uuid4().hex}",
        "_pinned_provider_id": route["provider_id"],
        "_pinned_model_id": route["model_id"],
        "_pinned_effective_reasoning": route["effective_reasoning"],
        "_route_bound_at_accept": True,
        "_execution_owner": "desktop",
        "_execution_owner_pid": os.getpid(),
        "_execution_owner_process_identity": owner_process_identity,
    }


def _run_turn_to_terminality(agent, user_input: str, *, turn_id: str, **kwargs):
    """Use the verified lifecycle while retaining narrow legacy-test adapters."""

    runner = getattr(agent, "run_terminal_turn", None)
    if callable(runner):
        return runner(user_input, turn_id=turn_id, **kwargs)
    kwargs.pop("semantic_objective", None)
    return agent.run(user_input, **kwargs)


def _terminality_result(agent, turn_id: str, final_text: str = "") -> dict | None:
    checker = getattr(agent, "terminality_result", None)
    if callable(checker):
        return checker(turn_id)
    return {
        "terminal_state": "success_verified",
        "final_text": str(final_text or ""),
    }


def _turn_persistence_metadata(
    turn_meta: dict,
    *,
    role: str,
    extra: dict | None = None,
) -> dict:
    message_key = "user_message_id" if role == "user" else "assistant_message_id"
    metadata = dict(extra or {})
    metadata.update({
        "_conversation_id": str(turn_meta.get("conversation_id") or ""),
        "_request_id": str(turn_meta.get("request_id") or ""),
        "_client_request_id": str(turn_meta.get("request_id") or ""),
        "_turn_id": str(turn_meta.get("turn_id") or ""),
        "_run_id": str(
            turn_meta.get("run_id") or turn_meta.get("turn_id") or ""
        ),
        "_provider_id": str(turn_meta.get("provider_id") or ""),
        "_model_id": str(turn_meta.get("model_id") or ""),
        "_effective_reasoning": str(
            turn_meta.get("effective_reasoning") or ""
        ),
        "_message_id": str(turn_meta.get(message_key) or ""),
        "_operation_id": str(turn_meta.get(message_key) or ""),
        "_execution_owner": str(
            turn_meta.get("_execution_owner") or "desktop"
        ),
        "_execution_owner_pid": int(
            turn_meta.get("_execution_owner_pid") or os.getpid()
        ),
        "_execution_owner_process_identity": str(
            turn_meta.get("_execution_owner_process_identity") or ""
        ),
    })
    return metadata


_DESKTOP_CHAT_BINDING_KEYS = (
    "conversation_id", "request_id", "turn_id", "run_id",
    "provider_id", "model_id", "effective_reasoning",
)


def _desktop_reserved_assistant_state(
    messages: list[dict],
    *,
    binding: dict,
    assistant_message_id: str,
    final_text: str,
) -> str:
    """Classify the exact reserved assistant as absent, exact, or conflicting."""

    exact_found = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        same_message_id = (
            str(message.get("_message_id") or "") == assistant_message_id
        )
        same_turn = all(
            str(message.get("_" + key) or "") == str(binding.get(key) or "")
            for key in (
                "conversation_id", "request_id", "turn_id", "run_id",
            )
        )
        if not same_message_id and not same_turn:
            continue
        exact = bool(
            same_message_id
            and str(message.get("content") or "") == final_text
            and all(
                str(message.get("_" + key) or "")
                == str(binding.get(key) or "")
                for key in _DESKTOP_CHAT_BINDING_KEYS
            )
        )
        if not exact or exact_found:
            return "conflict"
        exact_found = True
    return "exact" if exact_found else "absent"


def _persist_desktop_user_turn(
    conv_id: str,
    content: str,
    *,
    turn_meta: dict | None = None,
    metadata: dict | None = None,
    turn_directives=None,
    use_task_hierarchy: bool = False,
) -> dict:
    """Durably accept a desktop user turn before any agent/tool callback."""

    meta = turn_meta or _new_desktop_turn_meta(
        conv_id,
        turn_directives=turn_directives,
        task_text=content if use_task_hierarchy else "",
    )
    semantic_objective = str(content or "")[:32_000]
    user_metadata = dict(metadata or {})
    user_metadata.update({
        "_assistant_message_id": str(
            meta.get("assistant_message_id") or ""
        ),
        "_terminality_objective": semantic_objective,
    })
    meta["_terminality_objective"] = semantic_objective
    receipt = conversations.commit_message(
        conv_id,
        "user",
        content,
        metadata=_turn_persistence_metadata(
            meta, role="user", extra=user_metadata,
        ),
    )
    if receipt is None:
        raise RuntimeError("conversation persistence unavailable")
    meta["_user_commit"] = receipt
    if receipt.get("committed"):
        _notify_mobile_conversation_changed(
            conv_id,
            revision=receipt.get("revision", 0),
            operation_id=receipt.get("operation_id", ""),
        )
    return meta


def _incomplete_desktop_chat_turns() -> list[dict]:
    """Return only exact, non-replaying desktop Chat recovery attachments."""

    from osia.tool_attempts import (
        RECOVERABLE_STATUSES,
        TERMINAL_STATUSES,
        ToolAttemptStatus,
        get_tool_attempt_store,
    )
    from osia.turn_terminality import (
        TurnTerminalityController,
        live_process_identity,
        load_persisted_terminal_result,
    )

    store = get_tool_attempt_store()
    candidates_by_conversation: dict[str, list[dict]] = {}
    for summary in conversations.list_conversations():
        conv_id = str((summary or {}).get("id") or "")
        conversation = conversations.get_conversation(conv_id)
        messages = list((conversation or {}).get("messages") or [])
        if not conv_id or not messages:
            continue
        try:
            suspended = store.find_resumable_chat_turn(conv_id)
        except Exception:
            suspended = None
        suspended_status = None
        try:
            if isinstance(suspended, dict):
                suspended_status = ToolAttemptStatus(suspended.get("status"))
        except (TypeError, ValueError):
            suspended_status = None

        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            request_id = str(
                message.get("_request_id")
                or message.get("_client_request_id")
                or ""
            )
            turn_id = str(message.get("_turn_id") or "")
            run_id = str(message.get("_run_id") or "")
            assistant_message_id = str(
                message.get("_assistant_message_id") or ""
            )
            objective = message.get("_terminality_objective")
            if not isinstance(objective, str) or not objective.strip():
                continue
            if not all((request_id, turn_id, run_id, assistant_message_id)):
                continue
            if str(message.get("_conversation_id") or conv_id) != conv_id:
                continue
            execution_owner = str(message.get("_execution_owner") or "")
            if execution_owner != "desktop" and not (
                not execution_owner
                and request_id.startswith("desktop_req_")
                and turn_id.startswith("desktop_turn_")
            ):
                continue
            try:
                owner_pid = int(message.get("_execution_owner_pid") or 0)
            except (TypeError, ValueError, OverflowError):
                owner_pid = 0
            owner_identity = str(
                message.get("_execution_owner_process_identity") or ""
            )
            if owner_pid > 0:
                owner_alive, live_identity = live_process_identity(owner_pid)
                if owner_alive and (
                    not owner_identity
                    or not live_identity
                    or owner_identity == live_identity
                ):
                    # Another live desktop process still owns this accepted
                    # turn. Inventory is read-only with respect to that owner.
                    continue
            turn_directives = None
            raw_directives = message.get("_turn_directives_v1")
            if raw_directives is not None:
                try:
                    from osia.slash_commands import SlashTurnDirectivesV1

                    turn_directives = SlashTurnDirectivesV1.from_wire(
                        raw_directives
                    )
                except Exception:
                    continue
            persisted_terminal = load_persisted_terminal_result(turn_id)
            message_route = {
                "provider_id": str(message.get("_provider_id") or ""),
                "model_id": str(message.get("_model_id") or ""),
                "effective_reasoning": str(
                    message.get("_effective_reasoning") or ""
                ),
            }
            if any(not value for value in message_route.values()):
                continue
            message_binding = {
                "conversation_id": conv_id,
                "request_id": request_id,
                "turn_id": turn_id,
                "run_id": run_id,
                **message_route,
            }
            assistant_state = _desktop_reserved_assistant_state(
                messages[index + 1:],
                binding=message_binding,
                assistant_message_id=assistant_message_id,
                final_text=str(
                    (persisted_terminal or {}).get("final_text") or ""
                ).strip(),
            )
            terminal_binding = (
                persisted_terminal.get("execution_binding")
                if isinstance(persisted_terminal, dict) else None
            )
            terminal_binding_exact = bool(
                isinstance(terminal_binding, dict) and all(
                    str(terminal_binding.get(key) or "") == expected
                    for key, expected in (
                        ("conversation_id", conv_id),
                        ("request_id", request_id),
                        ("turn_id", turn_id),
                        ("run_id", run_id),
                    )
                ) and all(
                    str(terminal_binding.get(key) or "")
                    for key in (
                        "provider_id", "model_id", "effective_reasoning",
                    )
                ) and all(
                    str(terminal_binding.get(key) or "") == value
                    for key, value in message_route.items()
                )
            )
            if (
                terminal_binding_exact
                and persisted_terminal.get("terminal_state") == "user_cancelled"
            ):
                # Stop is already the durable terminal for this exact turn.
                # There is no assistant final to project, and reattaching it
                # would leave the conversation falsely busy after restart.
                continue
            if terminal_binding_exact:
                continuation_attempt_id = ""
                if isinstance(suspended, dict):
                    if (
                        suspended_status not in TERMINAL_STATUSES
                        or suspended.get("continuation_state") == "complete"
                        or str(suspended.get("owner_scope") or "chat") != "chat"
                        or any(
                            str(suspended.get(key) or "")
                            != str(terminal_binding.get(key) or "")
                            for key in (
                                "conversation_id", "request_id", "turn_id",
                                "provider_id", "model_id",
                            )
                        )
                        or str(
                            suspended.get("final_response_message_id") or ""
                        ) != assistant_message_id
                        or not str(suspended.get("tool_attempt_id") or "")
                    ):
                        continue
                    continuation_attempt_id = str(
                        suspended.get("tool_attempt_id") or ""
                    )
                if assistant_state == "conflict" or (
                    assistant_state == "exact" and not continuation_attempt_id
                ):
                    continue
                candidates_by_conversation.setdefault(conv_id, []).append({
                    "conversation_id": conv_id,
                    "objective": objective,
                    "history": messages[:index],
                    "turn_directives": turn_directives,
                    "persisted_terminal": dict(persisted_terminal),
                    "continuation_attempt_id": continuation_attempt_id,
                    "turn_meta": {
                        "conversation_id": conv_id,
                        "request_id": request_id,
                        "client_request_id": request_id,
                        "turn_id": turn_id,
                        "run_id": run_id,
                        "provider_id": str(terminal_binding["provider_id"]),
                        "model_id": str(terminal_binding["model_id"]),
                        "effective_reasoning": str(
                            terminal_binding["effective_reasoning"]
                        ),
                        "user_message_id": str(message.get("_message_id") or ""),
                        "assistant_message_id": assistant_message_id,
                        "_pinned_provider_id": str(
                            terminal_binding["provider_id"]
                        ),
                        "_pinned_model_id": str(terminal_binding["model_id"]),
                        "_pinned_effective_reasoning": str(
                            terminal_binding["effective_reasoning"]
                        ),
                        "_terminality_objective": objective,
                        "_accepted_user_already_persisted": True,
                        "_execution_owner": "desktop",
                        "_execution_owner_pid": owner_pid,
                        "_execution_owner_process_identity": owner_identity,
                    },
                })
                continue
            if assistant_state != "absent":
                # A correlated assistant without the exact durable reservation
                # is contradictory state; never replay or overwrite it.
                continue
            try:
                controller = TurnTerminalityController(
                    turn_id=turn_id,
                    objective=objective,
                    execution_required=True,
                )
                terminality = controller.snapshot()
            except Exception:
                continue
            setup_binding = terminality.get("execution_binding") or {}
            setup_binding_safe = bool(
                not setup_binding
                or (
                    isinstance(setup_binding, dict)
                    and all(
                        str(setup_binding.get(key) or "") == value
                        for key, value in message_binding.items()
                    )
                )
            )
            if (
                not isinstance(suspended, dict)
                and setup_binding_safe
                and not terminality.get("invocations")
                and not terminality.get("active_attempts")
                and not terminality.get("unknown_effects")
                and terminality.get("permission_state") != "pending"
                and not terminality.get("final_reservation")
                and not terminality.get("terminal_state")
            ):
                candidates_by_conversation.setdefault(conv_id, []).append({
                    "conversation_id": conv_id,
                    "objective": objective,
                    "history": messages[:index],
                    "turn_directives": turn_directives,
                    "needs_terminal_reservation": True,
                    "turn_meta": {
                        "conversation_id": conv_id,
                        "request_id": request_id,
                        "client_request_id": request_id,
                        "turn_id": turn_id,
                        "run_id": run_id,
                        **message_route,
                        "user_message_id": str(
                            message.get("_message_id") or ""
                        ),
                        "assistant_message_id": assistant_message_id,
                        "_pinned_provider_id": message_route["provider_id"],
                        "_pinned_model_id": message_route["model_id"],
                        "_pinned_effective_reasoning": message_route[
                            "effective_reasoning"
                        ],
                        "_terminality_objective": objective,
                        "_accepted_user_already_persisted": True,
                        "_execution_owner": "desktop",
                        "_execution_owner_pid": owner_pid,
                        "_execution_owner_process_identity": owner_identity,
                    },
                })
                continue
            binding = terminality.get("execution_binding") or {}
            if (
                not isinstance(suspended, dict)
                or (
                    suspended_status not in RECOVERABLE_STATUSES
                    and not (
                        suspended_status in TERMINAL_STATUSES
                        and suspended.get("continuation_state") != "complete"
                    )
                )
                or str(suspended.get("owner_scope") or "chat") != "chat"
                or not isinstance(binding, dict)
                or terminality.get("terminal_state")
                or terminality.get("final_reservation")
                or not isinstance(terminality.get("strategy_snapshot"), dict)
                or not terminality.get("strategy_snapshot")
                or any(
                    str(binding.get(key) or "") != expected
                    for key, expected in (
                        ("conversation_id", conv_id),
                        ("request_id", request_id),
                        ("turn_id", turn_id),
                        ("run_id", run_id),
                    )
                )
                or any(
                    not str(binding.get(key) or "")
                    for key in (
                        "provider_id", "model_id", "effective_reasoning",
                    )
                )
                or any(
                    str(suspended.get(key) or "") != expected
                    for key, expected in (
                        ("conversation_id", conv_id),
                        ("request_id", request_id),
                        ("turn_id", turn_id),
                        ("provider_id", str(binding["provider_id"])),
                        ("model_id", str(binding["model_id"])),
                        ("final_response_message_id", assistant_message_id),
                    )
                )
                or not str(suspended.get("tool_attempt_id") or "")
            ):
                continue
            candidates_by_conversation.setdefault(conv_id, []).append({
                "conversation_id": conv_id,
                "objective": objective,
                "history": messages[:index],
                "turn_directives": turn_directives,
                "turn_meta": {
                    "conversation_id": conv_id,
                    "request_id": request_id,
                    "client_request_id": request_id,
                    "turn_id": turn_id,
                    "run_id": run_id,
                    "provider_id": str(binding["provider_id"]),
                    "model_id": str(binding["model_id"]),
                    "effective_reasoning": str(
                        binding["effective_reasoning"]
                    ),
                    "user_message_id": str(message.get("_message_id") or ""),
                    "assistant_message_id": assistant_message_id,
                    "_pinned_provider_id": str(binding["provider_id"]),
                    "_pinned_model_id": str(binding["model_id"]),
                    "_pinned_effective_reasoning": str(
                        binding["effective_reasoning"]
                    ),
                    "_existing_tool_attempt_id": str(
                        suspended.get("tool_attempt_id") or ""
                    ),
                    "_terminality_objective": objective,
                    "_accepted_user_already_persisted": True,
                    "_execution_owner": "desktop",
                    "_execution_owner_pid": owner_pid,
                    "_execution_owner_process_identity": owner_identity,
                },
            })

    # More than one candidate for a conversation is contradictory state. Never
    # guess by recency: the exact one-active-turn invariant fails closed.
    return [
        items[0]
        for items in candidates_by_conversation.values()
        if len(items) == 1
    ]


def _persist_desktop_reserved_final(candidate: dict) -> dict | None:
    """Commit one already-reserved exact final without provider/action replay."""

    from osia.agent import strip_tool_artifacts

    persisted = (candidate or {}).get("persisted_terminal")
    turn_meta = dict((candidate or {}).get("turn_meta") or {})
    binding = (
        persisted.get("execution_binding")
        if isinstance(persisted, dict) else None
    )
    if not isinstance(binding, dict) or any(
        str(binding.get(key) or "") != str(turn_meta.get(key) or "")
        for key in (
            "conversation_id", "request_id", "turn_id", "run_id",
            "provider_id", "model_id", "effective_reasoning",
        )
    ):
        return None
    reserved_text = str(persisted.get("final_text") or "")
    display = strip_tool_artifacts(reserved_text).strip()
    if not display or display != reserved_text.strip():
        return None
    continuation_id = str(
        (candidate or {}).get("continuation_attempt_id") or ""
    )
    continuation_store = None
    continuation_claim = None
    try:
        from osia.execution_trace import (
            ExecutionTraceLedger,
            terminal_display_key,
            validate_persisted_trace,
        )
        from osia.tool_attempts import is_internal_tool_control_text

        if is_internal_tool_control_text(display):
            return None
        display_key = terminal_display_key(persisted.get("terminal_state"))
        if not display_key:
            return None
        trace = validate_persisted_trace(
            (candidate or {}).get("execution_trace")
            or turn_meta.get("_execution_trace_v1")
            or persisted.get("execution_trace")
        )
        if trace is not None and (
            any(
                str(trace.get(key) or "") != str(binding.get(key) or "")
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            )
            or not any(
                isinstance(event, dict) and event.get("terminal") is True
                for event in trace.get("events") or ()
            )
        ):
            trace = None
        if trace is None:
            ledger = ExecutionTraceLedger(**binding)
            ledger.record(
                kind="terminal",
                display_key=display_key,
                terminal=True,
                dedupe_key="terminal",
            )
            trace = validate_persisted_trace(ledger.snapshot())
        if trace is None:
            return None
        from osia.document_artifacts import normalize_references

        artifact_refs = normalize_references(
            (candidate or {}).get("artifact_refs")
            or turn_meta.get("_artifact_refs")
            or persisted.get("artifact_refs")
        )
        extra_metadata = {
            "_execution_trace_v1": trace,
            "_terminal_state": str(
                persisted.get("terminal_state") or ""
            ),
            "_terminal_reservation_id": str(
                persisted.get("reservation_id") or ""
            ),
        }
        if artifact_refs:
            extra_metadata["_artifact_refs"] = artifact_refs
        metadata = _turn_persistence_metadata(
            turn_meta,
            role="assistant",
            extra=extra_metadata,
        )
        if continuation_id:
            from osia.tool_attempts import (
                TERMINAL_STATUSES,
                ToolAttemptStatus,
                get_tool_attempt_store,
            )

            continuation_store = get_tool_attempt_store()
            attempt = continuation_store.get_attempt(continuation_id)
            if (
                not isinstance(attempt, dict)
                or ToolAttemptStatus(attempt.get("status"))
                not in TERMINAL_STATUSES
                or str(attempt.get("owner_scope") or "chat") != "chat"
                or any(
                    str(attempt.get(key) or "") != str(binding.get(key) or "")
                    for key in (
                        "conversation_id", "request_id", "turn_id",
                        "provider_id", "model_id",
                    )
                )
                or str(attempt.get("final_response_message_id") or "")
                != str(turn_meta.get("assistant_message_id") or "")
            ):
                return None
            continuation_claim = continuation_store.claim_continuation(
                continuation_id
            )
            if continuation_claim is None:
                return None
        receipt = conversations.commit_message(
            str(turn_meta.get("conversation_id") or ""),
            "assistant",
            display,
            metadata=metadata,
        )
        committed_conversation = (
            receipt.get("conversation")
            if isinstance(receipt, dict) else None
        )
        if receipt is not None and _desktop_reserved_assistant_state(
            list((committed_conversation or {}).get("messages") or []),
            binding=binding,
            assistant_message_id=str(
                turn_meta.get("assistant_message_id") or ""
            ),
            final_text=display,
        ) != "exact":
            raise ValueError("reserved assistant correlation conflict")
    except Exception:
        if continuation_store is not None and continuation_claim is not None:
            try:
                continuation_store.release_continuation(
                    continuation_id, continuation_claim,
                )
            except Exception:
                pass
        return None
    if receipt is None:
        if continuation_store is not None and continuation_claim is not None:
            try:
                continuation_store.release_continuation(
                    continuation_id, continuation_claim,
                )
            except Exception:
                pass
        return None
    if continuation_store is not None and continuation_claim is not None:
        try:
            completed = continuation_store.complete_continuation(
                continuation_id,
                continuation_claim,
                final_response_message_id=str(
                    turn_meta.get("assistant_message_id") or ""
                ),
            )
        except Exception:
            completed = False
        if not completed:
            try:
                continuation_store.release_continuation(
                    continuation_id, continuation_claim,
                )
            except Exception:
                pass
            return None
    if receipt.get("committed"):
        _notify_mobile_conversation_changed(
            str(turn_meta.get("conversation_id") or ""),
            revision=receipt.get("revision", 0),
            operation_id=receipt.get("operation_id", ""),
        )
    return {
        "binding": {**binding, "assistant_message_id": str(
            turn_meta.get("assistant_message_id") or ""
        )},
        "display": display,
        "trace": trace,
        "receipt": receipt,
    }


def _dispatch_after_persisted_user(
    conv_id: str,
    content: str,
    effect,
    *,
    turn_meta: dict | None = None,
    metadata: dict | None = None,
):
    """Testable persist-before-effect boundary used by desktop entry points."""

    meta = _persist_desktop_user_turn(
        conv_id,
        content,
        turn_meta=turn_meta,
        metadata=metadata,
    )
    return effect(meta)


def _notify_mobile_mcp_sync(server_id: str, action: str) -> None:
    """Best-effort desktopâ†’server MCP state mirror (Phase 1, direction A).

    After the DESKTOP manager connects/disconnects an MCP connector, tell the
    mobile WS server so it reconciles its OWN manager (headless, fresh-token
    gated â€” never a browser) and broadcasts the resulting status to every
    connected phone. Carries only ``{id, action}`` â€” no secret. Silent no-op when
    the server isn't running. Called from the GUI's MCP background threads (never
    the Qt main thread), so the 1.5s timeout never blocks the UI."""
    import json
    import urllib.request
    try:
        payload = json.dumps({"id": server_id, "action": action}).encode("utf-8")
        req = urllib.request.Request(
            _SERVER_MCP_SYNC_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1.5).close()
    except Exception as e:
        print(f"[MCP sync] mobile mcp_sync notify skipped: {e}")


def _notify_mobile_cron_notification(
    *,
    task_id: str | None,
    title: str,
    status: str,
    conversation_id: str | None,
) -> None:
    """Best-effort: ask the mobile server to broadcast a cron notification."""

    import json
    import urllib.request
    try:
        payload = json.dumps({
            "task_id": task_id or "",
            "title": title,
            "status": status,
            "conversation_id": conversation_id or "",
        }).encode("utf-8")
        req = urllib.request.Request(
            _SERVER_CRON_NOTIFICATION_URL,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1.5).close()
    except Exception as e:
        print(f"[CRON] mobile notification skipped: {e}")


def request_oauth_tunnel_connect(server_id: str) -> bool:
    """WOR-96 PART 1 â€” start an OAuth connect that completes via the HTTPS tunnel
    callback (app.osia.space â†’ the SERVER process), and open the authorize URL in the
    PC browser.

    POSTs ``{id}`` to the loopback trigger ``/internal/mcp_oauth_tunnel_connect``.
    The SERVER runs the OAuth provider IN ITS OWN process â€” where the tunnel
    redirect lands and the SDK's in-memory PKCE verifier/state live â€” and returns
    the authorize URL, which we open HERE in the PC browser (the desktop process
    is the one allowed to open a browser; the server neutralises
    ``webbrowser.open``). The token exchange completes server-side when the user
    finishes login and the provider redirects to ``/oauth/callback``.

    Returns True iff the browser was opened. Best-effort; never raises. This is a
    SEPARATE path from the existing loopback Connect button (unchanged): it runs no
    provider in the desktop process and gives the desktop no HTTP listener â€” it
    only kicks the server and opens the returned URL. Call OFF the Qt main thread
    (it blocks up to ~35s waiting for discovery+DCR)."""
    import json
    import urllib.request
    import webbrowser
    try:
        payload = json.dumps({"id": server_id}).encode("utf-8")
        req = urllib.request.Request(
            _SERVER_MCP_OAUTH_TUNNEL_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        # The server waits ~30s for discovery+DCR before returning the URL, so
        # allow a slightly larger read timeout than that bound.
        with urllib.request.urlopen(req, timeout=35.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WOR-96] oauth tunnel connect request failed: {e}")
        return False
    if not isinstance(data, dict) or not data.get("ok"):
        reason = data.get("error") if isinstance(data, dict) else data
        print(f"[WOR-96] oauth tunnel connect not started: {reason}")
        return False
    auth_url = data.get("auth_url")
    if not isinstance(auth_url, str) or not auth_url:
        print("[WOR-96] oauth tunnel connect: no authorize URL returned")
        return False
    try:
        webbrowser.open(auth_url)
        print("[WOR-96] opened OAuth authorize URL in the PC browser (tunnel flow).")
        return True
    except Exception as e:
        print(f"[WOR-96] could not open browser for OAuth tunnel flow: {e}")
        return False


def request_oauth_cancel(server_id: str) -> bool:
    """WOR-97 â€” desktopâ†’server bridge to abort an in-flight OAuth login that runs
    in the SERVER process (a tunnel/PART-1 flow the desktop manager can't cancel
    locally). POSTs ``{id}`` to the loopback ``/internal/mcp_oauth_cancel``; the
    server calls ``MCPManager.cancel_pending`` and broadcasts the reset so every
    pill repaints to disconnected.

    Carries only an id (no secret). Best-effort, never raises; a silent no-op when
    the server isn't running. Call OFF the Qt main thread (the 1.5s timeout never
    blocks the UI). Returns True iff the server acked ok. The desktop only uses
    this for connectors the LOOPBACK manager doesn't own â€” a PC-initiated login is
    cancelled directly via ``get_manager().cancel_pending``."""
    import json
    import urllib.request
    try:
        payload = json.dumps({"id": server_id}).encode("utf-8")
        req = urllib.request.Request(
            _SERVER_MCP_OAUTH_CANCEL_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(isinstance(data, dict) and data.get("ok"))
    except Exception as e:
        print(f"[WOR-97] oauth cancel request skipped: {e}")
        return False


def request_oauth_switch(server_id: str) -> bool:
    """WOR-98 â€” desktopâ†’server bridge for "Switch account" on a CONNECTED OAuth
    connector. POSTs ``{id}`` to the loopback ``/internal/mcp_oauth_switch``; the
    server tears the session down, REVOKES the grant (RFC 7009) AND forgets the DCR
    client blob (``delete_mcp_oauth``) so the next Connect re-runs DCR + a fresh
    login (a different account), then broadcasts the reset so every pill repaints to
    disconnected.

    Carries only an id (no secret). Best-effort, never raises; a silent no-op when
    the server isn't running. Call OFF the Qt main thread (the timeout never blocks
    the UI). Returns True iff the server acked ok. Used for BOTH loopback and tunnel
    OAuth connectors â€” the revoke+forget logic lives server-side and
    ``oauth_tokens.json`` is shared via CONFIG_DIR, so one bridge route serves both.
    The route returns immediately (the revoke/forget runs on a server daemon
    thread), so the short timeout is ample."""
    import json
    import urllib.request
    try:
        payload = json.dumps({"id": server_id}).encode("utf-8")
        req = urllib.request.Request(
            _SERVER_MCP_OAUTH_SWITCH_URL, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(isinstance(data, dict) and data.get("ok"))
    except Exception as e:
        print(f"[WOR-98] oauth switch request skipped: {e}")
        return False


def _ensure_mobile_server_windows() -> None:
    """On Windows, make sure the OSIA mobile server (run_server.py) is running.

    The server normally starts at login via its own per-user Scheduled Task
    ("OSIA Mobile Server", registered by ``install.bat``) â€” the Windows
    equivalent of the Linux ``osia-server.service`` systemd unit, fully
    independent of this GUI process. This call is a self-heal safety net: it
    registers the task if it is missing (e.g. an install that predates the task
    model) and starts the service when nothing is serving yet.

    Idempotent and safe: ``ensure_service_running`` only starts a task instance
    when neither the port (8765) nor a supervisor is already up, so it can never
    double-bind the port. No-op on every non-Windows platform.
    """
    if sys.platform != "win32":
        return
    # Persist the mobile pairing token to disk BEFORE launching the server, so
    # the detached server process reads an existing token instead of generating
    # a fresh one on first run (WOR-11 Windows: on a clean start the token must
    # survive restarts AND match the one the server uses). load_mobile_token()
    # returns "" when no token file exists yet; regenerate_mobile_token() then
    # creates one and writes config/mobile_token.txt. Idempotent (a no-op load
    # once the file exists) and best-effort â€” it must never block server startup.
    try:
        from osia.config import load_mobile_token, regenerate_mobile_token
        if not load_mobile_token():
            regenerate_mobile_token()
            print("[OSIA] mobile token generated and saved (Windows).")
    except Exception as e:
        print(f"[OSIA] mobile token ensure skipped: {e}")
    try:
        from osia.utils.systemd_manager import ensure_service_running
        if ensure_service_running("osia-server.service"):
            print("[OSIA] mobile server task ensured (Windows).")
        else:
            print("[OSIA] could not ensure the mobile server (Windows).")
    except Exception as e:
        print(f"[OSIA] mobile server autostart skipped: {e}")


def _persist_agent_delta(
    conv_id,
    agent,
    prev_history_len,
    *,
    user_display_text: str | None = None,
    turn_meta: dict | None = None,
) -> bool:
    """Persist messages added to ``agent.messages`` during the most recent
    ``agent.run()`` call. ``prev_history_len`` is the length of the
    non-system message list (``agent.messages[1:]``) before that call.

    Return True only when the canonical assistant delta was durably committed.
    Callers must not project a terminal answer or clear its busy state on False.
    """
    if not conv_id:
        return False
    try:
        new_msgs = agent.messages[1:][prev_history_len:]
        canonical_assistant_committed = False
        pending_user_display = user_display_text
        final_assistant_index = next(
            (
                index for index in range(len(new_msgs) - 1, -1, -1)
                if isinstance(new_msgs[index], dict)
                and new_msgs[index].get("role") == "assistant"
                and not new_msgs[index].get("_internal")
                and not new_msgs[index].get("_provisional")
            ),
            None,
        )
        from osia.document_artifacts import normalize_references
        artifact_refs = normalize_references(
            getattr(agent, "last_run_artifacts", None)
        )
        for index, m in enumerate(new_msgs):
            # WOR-83: internal model-steering frames (anti-loop / hallucination
            # / empty-response guards) carry an _internal marker and must NEVER
            # be persisted. add_message stores only role+content, so a stored
            # steering frame loses its marker and resurfaces on reload as the
            # user's own bubble â€” in the desktop popup AND on mobile, which
            # share this conversation store. This is the desktop counterpart of
            # the server's exported_history() filter (the single persistence
            # boundary for the local app).
            clean = _sanitize_persisted_history_message(m)
            if clean is None and isinstance(m, dict) and m.get("_document_private"):
                if (
                    m.get("role") == "user"
                    and pending_user_display is not None
                    and not m.get("_internal")
                ):
                    clean = {"role": "user", "content": pending_user_display}
                    pending_user_display = None
                elif m.get("role") == "assistant" and index == final_assistant_index:
                    visible = dict(m)
                    visible.pop("_document_private", None)
                    clean = _sanitize_persisted_history_message(visible)
            if clean is None:
                continue
            if turn_meta is not None and clean.get("role") == "user":
                # The desktop entry points durably accepted this user turn
                # before agent.run(); never append it a second time here.
                continue
            if (
                turn_meta is not None
                and clean.get("role") == "assistant"
                and index != final_assistant_index
            ):
                continue
            if pending_user_display is not None and clean.get("role") == "user":
                clean = dict(clean)
                clean["content"] = pending_user_display
                pending_user_display = None
            context = getattr(agent, "turn_context", None)
            metadata = None
            if context is not None:
                metadata = context.message_metadata()
            if (
                clean.get("role") == "user"
                and isinstance(clean.get("_attachments"), list)
                and clean.get("_attachments")
            ):
                metadata = dict(metadata or {})
                metadata["_attachments"] = list(clean["_attachments"])
            if artifact_refs and index == final_assistant_index:
                metadata = dict(metadata or {})
                metadata["_artifact_refs"] = artifact_refs
            if turn_meta is not None and clean.get("role") == "assistant":
                terminal = _terminality_result(
                    agent,
                    str(turn_meta.get("turn_id") or ""),
                )
                terminal_state = str(
                    (terminal or {}).get("terminal_state") or ""
                )
                terminal_reservation_id = str(
                    (terminal or {}).get("reservation_id") or ""
                )
                if (
                    not isinstance(terminal, dict)
                    or not terminal_state
                    or not terminal_reservation_id
                    or any(
                        str(terminal.get(key) or "")
                        != str(turn_meta.get(key) or "")
                        for key in _DESKTOP_CHAT_BINDING_KEYS
                    )
                ):
                    raise ValueError(
                        "desktop assistant has no exact terminal reservation"
                    )
                trace_snapshot = None
                trace_getter = getattr(agent, "execution_trace_snapshot", None)
                if callable(trace_getter):
                    try:
                        trace_snapshot = trace_getter(
                            str(turn_meta.get("turn_id") or "")
                        )
                    except Exception:
                        trace_snapshot = None
                if isinstance(trace_snapshot, dict):
                    try:
                        from osia.execution_trace import validate_persisted_trace

                        trace_snapshot = validate_persisted_trace(trace_snapshot)
                    except Exception:
                        trace_snapshot = None
                if trace_snapshot is not None:
                    metadata = dict(metadata or {})
                    metadata["_execution_trace_v1"] = trace_snapshot
                metadata = dict(metadata or {})
                metadata.update({
                    "_terminal_state": terminal_state,
                    "_terminal_reservation_id": terminal_reservation_id,
                })
                metadata = _turn_persistence_metadata(
                    turn_meta,
                    role="assistant",
                    extra=metadata,
                )
            persistence_kwargs = {"metadata": metadata} if metadata else {}
            if turn_meta is None:
                updated = conversations.add_message(
                    conv_id, clean.get("role"), clean.get("content", ""),
                    **persistence_kwargs,
                )
                receipt = ({
                    "committed": True,
                    "revision": int((updated or {}).get("revision") or 0),
                    "operation_id": str(
                        (updated or {}).get("last_operation_id") or ""
                    ),
                } if updated else None)
            else:
                receipt = conversations.commit_message(
                    conv_id, clean.get("role"), clean.get("content", ""),
                    **persistence_kwargs,
                )
            if receipt is not None and clean.get("role") == "assistant":
                if turn_meta is None:
                    canonical_assistant_committed = True
                else:
                    committed_conversation = (
                        receipt.get("conversation")
                        if isinstance(receipt, dict) else None
                    )
                    committed_messages = list(
                        (committed_conversation or {}).get("messages") or []
                    )
                    assistant_message_id = str(
                        turn_meta.get("assistant_message_id") or ""
                    )
                    exact_assistants = [
                        item for item in committed_messages
                        if isinstance(item, dict)
                        and item.get("role") == "assistant"
                        and str(item.get("_message_id") or "")
                        == assistant_message_id
                    ]
                    canonical_assistant_committed = bool(
                        len(exact_assistants) == 1
                        and _desktop_reserved_assistant_state(
                            committed_messages,
                            binding=turn_meta,
                            assistant_message_id=assistant_message_id,
                            final_text=str(clean.get("content") or ""),
                        ) == "exact"
                        and str(
                            exact_assistants[0].get("_terminal_state") or ""
                        ) == terminal_state
                        and str(
                            exact_assistants[0].get(
                                "_terminal_reservation_id"
                            ) or ""
                        ) == terminal_reservation_id
                    )
            if receipt and receipt.get("committed"):
                _notify_mobile_conversation_changed(
                    conv_id,
                    revision=receipt.get("revision", 0),
                    operation_id=receipt.get("operation_id", ""),
                )
        return canonical_assistant_committed
    except Exception as e:
        print(
            "[OSIA] failed to persist conversation: "
            f"{type(e).__name__}"
        )
        return False


def _finalize_controlled_refusal(
    error,
    agent,
    conv_id,
    prev_history_len,
    emit_final,
    *,
    user_display_text: str | None = None,
    turn_meta: dict | None = None,
) -> str | None:
    """Persist and emit one explicitly classified refusal as assistant text."""

    if not (
        bool(getattr(error, "controlled_refusal", False))
        and bool(getattr(error, "native_action", False))
    ):
        return None
    text = str(error)
    agent.messages.append({"role": "assistant", "content": text})
    persistence_kwargs = {"user_display_text": user_display_text}
    if turn_meta is not None:
        persistence_kwargs["turn_meta"] = turn_meta
    if not _persist_agent_delta(
        conv_id,
        agent,
        prev_history_len,
        **persistence_kwargs,
    ):
        return None
    from osia.agent import strip_tool_artifacts

    display = strip_tool_artifacts(text)
    emit_final(conv_id, display)
    return display


def _persist_exchange(conv_id, user_text, assistant_text, turn_context=None):
    """Persist a stateless single-turn exchange (used by voice_mode,
    which currently calls run_agent() without keeping history).
    """
    if not conv_id:
        return
    try:
        metadata = None
        if turn_context is not None:
            metadata = turn_context.message_metadata()
        user_receipt = conversations.commit_message(
            conv_id, "user", user_text, metadata=metadata,
        )
        assistant_receipt = conversations.commit_message(
            conv_id, "assistant", assistant_text or "", metadata=metadata,
        )
        for receipt in (user_receipt, assistant_receipt):
            if receipt and receipt.get("committed"):
                _notify_mobile_conversation_changed(
                    conv_id,
                    revision=receipt.get("revision", 0),
                    operation_id=receipt.get("operation_id", ""),
                )
    except Exception as e:
        print(
            "[OSIA] failed to persist conversation: "
            f"{type(e).__name__}"
        )


# â”€â”€ Ctrl+Alt+P manual voice shortcut â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _start_ctrl_alt_p_listener(callback):
    """
    Start a global keyboard listener that triggers `callback` on Ctrl+Alt+P.
    This is the intentional release activation path while acoustic wake is off.

    Returns a stop function.
    """
    from pynput import keyboard

    def activate():
        try:
            print("âŒ¨ï¸  Ctrl+Alt+P pressed â€” triggering OSIA")
            callback()
        except Exception as exc:
            print(
                "[wake] Ctrl+Alt+P callback failed: "
                f"{type(exc).__name__}",
                flush=True,
            )

    hotkeys = keyboard.GlobalHotKeys({"<ctrl>+<alt>+p": activate})
    hotkeys.daemon = True
    hotkeys.start()
    return hotkeys.stop


def _start_popup_hotkey_listener(callback):
    """
    Start a global Ctrl+Alt+O listener that triggers `callback` (toggle the
    popup). This is the recovery path so the chat/settings GUI stays reachable
    even when the floating capsule is hidden (WOR-22 "show_capsule" toggle).

    Uses pynput's GlobalHotKeys, which canonicalises the modifier+letter combo
    reliably. `callback` runs on the listener thread, so the caller must hand in
    a thread-safe sink (a Qt signal's ``emit``). Returns a stop function, or
    ``None`` if the listener could not be started (never raises).
    """
    try:
        from pynput import keyboard
        hotkeys = keyboard.GlobalHotKeys({"<ctrl>+<alt>+o": callback})
        hotkeys.daemon = True
        hotkeys.start()
        return hotkeys.stop
    except Exception as e:
        print(f"[OSIA] could not start Ctrl+Alt+O popup hotkey: {e}")
        return None


# â”€â”€ Voice shutdown lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _shutdown_voice_runtime(
    *,
    close_acceptance,
    cancel_pending_activation,
    signal_worker_cancel,
    invalidate_tts,
    get_active_worker,
    stop_before_tts_shutdown=(),
    shutdown_tts,
    release_voice_gate,
    join_timeout: float = 1.0,
) -> bool:
    """Cancel voice ownership before shutting down TTS, with a bounded join."""

    close_acceptance()
    cancel_pending_activation()
    signal_worker_cancel()
    invalidate_tts()

    worker = get_active_worker()
    if (
        worker is not None
        and worker is not threading.current_thread()
        and worker.is_alive()
    ):
        worker.join(timeout=max(0.0, join_timeout))
    worker_stopped = worker is None or not worker.is_alive()

    for stop_callback in stop_before_tts_shutdown:
        try:
            stop_callback()
        except Exception as exc:
            print(
                "[shutdown] owned service stop failed "
                f"({type(exc).__name__})",
                flush=True,
            )
    try:
        shutdown_tts()
    finally:
        release_voice_gate()
    return worker_stopped


# â”€â”€ Modes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def text_mode():
    """Interactive text-based chat with OSIA."""
    agent = OSIAAgent()
    settings = load_settings()
    selected_model = settings.get("selected_model")
    if selected_model:
        agent.set_model(
            selected_model,
            provider=settings.get("selected_model_provider") or None,
        )
    # No conversation is auto-created â€” one is created lazily when the
    # first message is sent, so the stored list never fills with empties.
    active_conv = None
    print("=" * 60)
    print("  OSIA â€” Local AI Voice Assistant (text mode)")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print("  Type 'exit' or 'quit' to stop, 'reset' to clear history")
    print("=" * 60)

    while True:
        try:
            user_input = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("Goodbye!")
            break

        if user_input.lower() == "reset":
            agent.reset()
            active_conv = None
            print("[History cleared â€” next message starts a new conversation]")
            continue

        # First message of this conversation â€” create it now, never earlier.
        if active_conv is None:
            active_conv = conversations.create_conversation()
            _notify_mobile_conversation_changed(
                active_conv["id"],
                revision=active_conv.get("revision", 0),
                operation_id=active_conv.get("last_operation_id", ""),
            )
            print(f"[New conversation: {active_conv['id']}]")

        prev_history_len = len(agent.messages) - 1  # skip system prompt
        turn_meta = _new_desktop_turn_meta(active_conv["id"])
        print("\nOSIA is thinking...", flush=True)
        try:
            response = _dispatch_after_persisted_user(
                active_conv["id"],
                user_input,
                lambda _meta: _run_turn_to_terminality(
                    agent,
                    user_input,
                    turn_id=str(turn_meta.get("turn_id") or ""),
                    current_conversation_id=active_conv["id"],
                ),
                turn_meta=turn_meta,
            )
            if not _terminality_result(
                agent, str(turn_meta.get("turn_id") or ""), response,
            ):
                raise RuntimeError("turn did not reach verified terminality")
            print(f"\nOSIA: {response}")
            if not _persist_agent_delta(
                active_conv["id"],
                agent,
                prev_history_len,
                turn_meta=turn_meta,
            ):
                raise RuntimeError("canonical assistant persistence failed")
        except Exception as e:
            print(f"\n[ERROR] {type(e).__name__}")


def voice_mode():
    """Single-shot voice mode: listen â†’ agent â†’ speak."""
    from osia import i18n
    from osia.config import load_settings
    from osia.turn_context import localized_text, resolve_turn_context
    from osia.voice.stt import listen
    from osia.voice.tts import speak
    from osia.agent import clean_response

    # No conversation is auto-created â€” one is created lazily when the
    # first spoken command arrives.
    active_conv = None
    print("=" * 60)
    print("  OSIA â€” Voice Mode")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print("=" * 60)

    while True:
        print("\n" + "-" * 40)
        print("Press Enter to start speaking (or type 'quit' to exit)...")
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if cmd.lower() in ("quit", "exit"):
            break

        user_text = listen(duration=5)
        if not user_text:
            print("âŒ Nothing heard.")
            stored = (
                conversations.get_conversation(active_conv["id"])
                if active_conv is not None else None
            )
            history = list((stored or {}).get("messages") or [])
            fallback_context = resolve_turn_context(
                "",
                history=history,
                saved_language=i18n.get_language(),
                reference=(active_conv or {}).get("id"),
            )
            speak(
                localized_text("stt_not_understood", fallback_context.language),
                language=fallback_context.language,
            )
            continue

        print(f"[STT] accepted voice input ({len(user_text)} chars).")

        # First message â€” create the conversation now, never earlier.
        if active_conv is None:
            active_conv = conversations.create_conversation()
            _notify_mobile_conversation_changed(
                active_conv["id"],
                revision=active_conv.get("revision", 0),
                operation_id=active_conv.get("last_operation_id", ""),
            )
            print(f"[New conversation: {active_conv['id']}]")

        print("ðŸ¤” Thinking...")
        turn_agent = OSIAAgent()
        settings = load_settings()
        selected_model = settings.get("selected_model")
        if selected_model:
            turn_agent.set_model(
                selected_model,
                provider=settings.get("selected_model_provider") or None,
            )
        stored = conversations.get_conversation(active_conv["id"])
        history = list((stored or {}).get("messages") or [])
        turn_context = resolve_turn_context(
            user_text,
            history=history,
            saved_language=i18n.get_language(),
            reference=active_conv["id"],
        )
        turn_meta = _new_desktop_turn_meta(active_conv["id"])
        try:
            response = _dispatch_after_persisted_user(
                active_conv["id"],
                user_text,
                lambda _meta: _run_turn_to_terminality(
                    turn_agent,
                    user_text,
                    turn_id=str(turn_meta.get("turn_id") or ""),
                    history=history,
                    current_conversation_id=active_conv["id"],
                    turn_context=turn_context,
                ),
                turn_meta=turn_meta,
                metadata=turn_context.message_metadata(),
            )
        except Exception as exc:
            print(f"[voice] turn failed: {type(exc).__name__}")
            continue
        response = clean_response(response)
        if not _terminality_result(
            turn_agent,
            str(turn_meta.get("turn_id") or ""),
            response,
        ):
            print("[voice] non-terminal result retained without finalization")
            continue
        print(f"[voice] assistant response accepted ({len(response)} chars).")

        if not _persist_agent_delta(
            active_conv["id"],
            turn_agent,
            len(history),
            user_display_text=user_text,
            turn_meta=turn_meta,
        ):
            print("[voice] canonical assistant persistence failed")
            continue
        print(f"[TTS] main.voice_mode: calling speak() ({len(response)} chars)", flush=True)
        speak(response, language=turn_context.speech_language)
        print("[TTS] main.voice_mode: speak() returned", flush=True)


def wake_mode():
    """Desktop voice mode with intentional Ctrl+Alt+P manual activation."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QObject, pyqtSignal, Qt, QTimer
    from PyQt6.QtGui import QGuiApplication, QIcon

    from osia.perf import perf_mark
    perf_mark("wake_mode: enter")

    from osia.utils.assets import ICON_PATH, ensure_desktop_entry
    from osia.gui.dynamic_island import DynamicIsland
    from osia.gui.main_popup import MainPopup
    from osia.voice.wake import VoiceSessionLifecycle, WakeAcceptanceGate
    # NOTE: osia.voice.stt (faster_whisper, ~4.3s to import on Windows) is NOT
    # imported here. listen_for_command is only used on the wake daemon thread â€”
    # it is imported lazily inside handle_wake()'s process() worker so the heavy
    # import never blocks GUI startup (WOR perf fix).
    from osia.voice.commands import is_new_chat_command
    from osia.voice.tts import (
        shutdown as tts_shutdown,
        playback_active as tts_playback_active,
        speak,
        speak_greeting_cached,
        speak_streaming,
        start_tts_session,
        start_streaming_session,
        stop as tts_stop,
        wait_for_playback_complete,
    )
    from osia.agent import clean_response, strip_tool_artifacts
    from osia import i18n
    from osia.turn_context import (
        localized_text,
        resolve_turn_context,
    )
    from osia import scheduled_tasks
    from osia.config import (
        ACOUSTIC_WAKE_AVAILABLE,
        ACOUSTIC_WAKE_UNAVAILABLE_REASON,
        load_settings,
        set_model_selection,
        set_setting,
    )
    from osia.models import discover_models_async
    from osia.cron import CronManager
    from osia.heartbeat import HeartbeatMonitor
    perf_mark("wake_mode: heavy imports done")

    class Bridge(QObject):
        wake_detected = pyqtSignal(int)
        remote_models_signal = pyqtSignal(list)
        # Update the chat with a (role, text) pair from a worker thread.
        chat_message = pyqtSignal(str, str)
        # Update the chat only if the popup is still viewing this conversation.
        chat_message_for_conversation = pyqtSignal(str, str, str)
        # Presentation-only desktop assistant streaming. Sentence chunks extend
        # one bounded typewriter target; final/error frames flush it exactly.
        assistant_stream_chunk = pyqtSignal(dict, str)
        assistant_stream_final = pyqtSignal(dict, str)
        assistant_stream_error = pyqtSignal(dict, str)
        chat_turn_started = pyqtSignal(dict)
        chat_cancel_finished = pyqtSignal(dict, object, bool, bool)
        execution_trace = pyqtSignal(dict)
        # Request GUI reprojection without carrying a possibly stale state.
        island_state = pyqtSignal()
        # Per-conversation desktop busy state for the popup input controls.
        conversation_busy_changed = pyqtSignal(str, bool)
        tool_attempt_status = pyqtSignal(dict, str)
        # Refresh the conversations panel (new active id) from a worker thread.
        conversations_changed = pyqtSignal(str)
        # Canonical conversation deletion completes off the GUI thread.
        conversation_delete_finished = pyqtSignal(dict)
        slash_information_ready = pyqtSignal(int, dict)
        context_compaction_ready = pyqtSignal(int, str, dict)
        # Reset chat (agent + GUI) from a worker thread â€” "nouveau chat" voice
        # command (WOR-10). Runs on the GUI thread so Qt calls are safe.
        new_chat = pyqtSignal()
        # Toggle the popup from the global Ctrl+Alt+O hotkey thread (WOR-22).
        # Marshalled onto the GUI thread so popup.toggle() is Qt-safe.
        toggle_popup = pyqtSignal()
        # Sanitized durable execution state projected into its originating Chat.

    bridge = Bridge()

    # Identify the app to the windowing system *before* any window is
    # created, so the taskbar/dock shows "OSIA" + the logo instead of
    # "python"/"osia_desktop" and a generic icon.
    #   â€¢ applicationName / displayName  â†’ the window title + dock label
    #   â€¢ desktopFileName ("osia")       â†’ Wayland/XCB map the window to
    #     ~/.local/share/applications/osia.desktop for its taskbar icon
    #   â€¢ setWindowIcon (absolute path)  â†’ the icon every window inherits,
    #     resolved from the project root so it works from any CWD. Uses the
    #     tightly-cropped ICON_PATH (not the padded logo) so the taskbar/dock
    #     shows the swirl at a proper visible size.
    ensure_desktop_entry()
    QGuiApplication.setDesktopFileName("osia")

    # HiDPI: crisp logo/icon rendering on Windows fractional-scaling displays
    # (125%/150%/175%). Qt6 always enables high-DPI scaling and DPI-aware pixmaps
    # (the old Qt.AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps attributes are
    # no-ops) and already defaults to PassThrough rounding, so the actual fix is
    # the per-pixmap setDevicePixelRatio scaling in main_popup.py /
    # dynamic_island.py (which renders the logo at native resolution). We pin
    # PassThrough explicitly to document the fractional-scale rounding those
    # DPR-tagged pixmaps assume â€” it equals the Qt6 default, so it is a no-op on
    # every platform today (integer-scale Linux/Mac included) and changes no
    # behaviour. Must run before the QApplication is constructed. (WOR-11.)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("OSIA")
    from osia.version import current_product_version, display_version
    app.setApplicationVersion(current_product_version())
    app.setApplicationDisplayName(
        f"OSIA Desktop {display_version(app.applicationVersion())} — Public Beta"
    )
    app.setWindowIcon(QIcon(ICON_PATH))

    # WOR-11 (Windows): bring up the mobile server alongside the GUI. On Linux a
    # systemd unit owns it; on Windows main.py does, so it also starts on the
    # login-autostart path (which only runs main.py). Idempotent + off-thread so
    # it never blocks GUI startup. No-op on non-Windows platforms.
    threading.Thread(target=_ensure_mobile_server_windows, daemon=True).start()

    island = DynamicIsland()
    popup = MainPopup()
    perf_mark("wake_mode: GUI constructed (island + popup)")

    # The release-only gate follows the acceptance flags in the legal manifest.
    def _run_release_legal_gate() -> None:
        from osia.gui.legal_dialog import maybe_show_release_legal_gate

        if not maybe_show_release_legal_gate(popup):
            app.quit()
            return
        try:
            from osia.onboarding import claim_automatic_onboarding

            if claim_automatic_onboarding():
                popup.show_onboarding(automatic=True)
        except Exception as e:
            # A damaged/read-only marker must never block normal startup. Its
            # absence is treated as an existing installation, so there is no
            # fallback that could surprise an established user with the guide.
            print(f"[ONBOARDING] first-install check skipped: {e}")

    QTimer.singleShot(0, _run_release_legal_gate)

    # Toggle the popup when the island is clicked; the popup opens below
    # the capsule's current (possibly dragged) position.
    popup.set_anchor(island)
    island.clicked.connect(popup.toggle)

    # Agent + active conversation state. No conversation is auto-created:
    # one is created only when the user clicks "New chat" or sends a first
    # message, so the list can legitimately be (and stay) empty.
    agent = OSIAAgent()
    active_conv = None
    selection_epoch = [0]
    pending_delete_ids: set[str] = set()
    pending_delete_contexts: dict[str, dict] = {}

    settings = load_settings()
    initial_model = settings.get("selected_model")
    initial_provider = settings.get("selected_model_provider") or None
    if initial_model:
        try:
            agent.set_model(initial_model, provider=initial_provider)
        except Exception as e:
            print(f"[DEBUG] set_model({initial_model!r}) failed: {e}")
    popup.set_current_model(agent.model, agent.provider)

    # WOR-44: auto-reconnect every ``enabled`` MCP connector at launch, so a
    # connector the user previously connected (its token / OAuth blob persisted)
    # is live again WITHOUT reopening Settings and clicking Connect. Reuses the
    # SAME MCPManager.connect() path as the Connect button â€” idempotent, and the
    # discovered tools register into the agent maps inside connect(), so they are
    # callable on the next turn. OAuth is allowed here (allow_oauth=True), but the
    # routine only attempts an OAuth connector whose stored token is still
    # comfortably valid; a stale token is SKIPPED rather than popping a browser at
    # startup (the SDK has no headless refresh on a restart 401). connect() blocks
    # (bounded), so the whole sweep runs on a daemon thread and never touches the
    # Qt main thread. Best-effort: a failure must never block GUI startup.
    def _reconnect_mcp_connectors() -> None:
        try:
            from osia.mcp_client.reconnect import reconnect_enabled_connectors
            reconnect_enabled_connectors(allow_oauth=True)
        except Exception as e:
            print(f"[OSIA] MCP auto-reconnect skipped: {e}")
    threading.Thread(
        target=_reconnect_mcp_connectors, name="osia-mcp-reconnect", daemon=True
    ).start()

    # WOR-44 Phase 2 (direction B, mobileâ†’PC): an ALWAYS-ON daemon that reconciles
    # THIS desktop process's MCPManager to the shared mcp_servers[].enabled flag.
    # When the user connects/disconnects an MCP connector FROM THE MOBILE app, the
    # server drives its own manager AND now persists enabled (Phase-2 server-side
    # write); this loop (~3s) sees the delta and connects/disconnects the desktop
    # manager to match â€” so the desktop mirrors mobile-side changes even when the
    # popup is CLOSED (the Settings QTimer stops then, and the desktop has no HTTP
    # listener, so polling on that timer is insufficient). reconcile_to_enabled
    # keeps the STRICT OAuth gate (trust_present_token=False), so this UNATTENDED
    # daemon NEVER pops a browser: a non-fresh OAuth connector simply stays
    # disconnected until the user re-auths on the PC. Cheap when idle (an
    # os.stat-cached settings read + per-server status lookups; no manager work
    # unless a flag changed) and idempotent (overlapping a boot reconnect / manual
    # Connect can't double-connect). Off the Qt main thread; a failure never
    # blocks GUI startup. When the popup is open, the existing Settings QTimer
    # repaints the pills from this SAME manager, so a daemon-driven change shows up
    # within its poll interval â€” no GUI change needed.
    def _reconcile_mcp_loop() -> None:
        while True:
            try:
                from osia.mcp_client.reconnect import reconcile_to_enabled
                reconcile_to_enabled(allow_oauth=True)
            except Exception as e:
                print(f"[OSIA] MCP reconcile tick skipped: {e}")
            time.sleep(3.0)
    threading.Thread(
        target=_reconcile_mcp_loop, name="osia-mcp-reconcile", daemon=True
    ).start()

    # WOR-22: honour the saved "show the floating capsule" preference at launch.
    island.set_capsule_visible(settings.get("show_capsule", True))

    # Serialises voice/cron/audio side effects. Typed chat turns are gated per
    # conversation below so different conversations can run independently.
    handler_lock = threading.Lock()
    busy_conversations: set[str] = set()
    busy_turn_keys: dict[str, tuple[str, str, str, str]] = {}
    busy_lock = threading.Lock()

    from osia.gui.island_lifecycle import IslandLifecycle

    island_lifecycle = IslandLifecycle(bridge.island_state.emit)

    def _any_conversation_busy() -> bool:
        with busy_lock:
            return bool(busy_conversations)

    wake_gate = WakeAcceptanceGate(
        handler_lock,
        is_busy=_any_conversation_busy,
        is_tts_active=tts_playback_active,
    )
    voice_ui_lifecycle = VoiceSessionLifecycle()

    # WOR-211 release contract: no detector is constructed and no idle
    # microphone stream is opened. Ctrl+Alt+P enters the same guarded voice
    # workflow intentionally; this does not depend on a missing model file.
    assert not ACOUSTIC_WAKE_AVAILABLE
    print(
        "[wake] acoustic activation unavailable: "
        f"{ACOUSTIC_WAKE_UNAVAILABLE_REASON}. Use Ctrl+Alt+P."
    )
    manual_activation_open = threading.Event()
    manual_activation_open.set()
    voice_shutdown_requested = threading.Event()
    voice_worker_lock = threading.Lock()
    active_voice_worker = [None]

    def _get_active_voice_worker():
        with voice_worker_lock:
            return active_voice_worker[0]

    def _start_voice_worker(
        target,
        *,
        name: str,
        on_start_failure=None,
    ) -> None:
        def run() -> None:
            try:
                target()
            finally:
                current = threading.current_thread()
                with voice_worker_lock:
                    if active_voice_worker[0] is current:
                        active_voice_worker[0] = None

        worker = threading.Thread(target=run, daemon=True, name=name)
        with voice_worker_lock:
            active_voice_worker[0] = worker
        try:
            worker.start()
        except Exception as exc:
            with voice_worker_lock:
                if active_voice_worker[0] is worker:
                    active_voice_worker[0] = None
            print(
                "[wake] voice worker could not start: "
                f"{type(exc).__name__}",
                flush=True,
            )
            if callable(on_start_failure):
                try:
                    on_start_failure()
                except Exception as finalize_error:
                    print(
                        "[wake] voice start-failure cleanup failed: "
                        f"{type(finalize_error).__name__}",
                        flush=True,
                    )

    def _manual_wake() -> None:
        if not manual_activation_open.is_set():
            print("[wake] manual activation rejected: shutting_down", flush=True)
            return
        if not wake_gate.reserve(0):
            print("[wake] manual activation rejected: busy", flush=True)
            return
        print("[wake] manual activation accepted", flush=True)
        bridge.wake_detected.emit(0)

    stop_manual_hotkey = _start_ctrl_alt_p_listener(_manual_wake)

    active_chat_turns: dict[tuple[str, str, str, str], dict] = {}
    active_chat_turns_lock = threading.Lock()

    def _chat_turn_key(binding: dict | None) -> tuple[str, str, str, str] | None:
        raw = binding if isinstance(binding, dict) else {}
        values = tuple(
            str(raw.get(name) or "")
            for name in ("conversation_id", "request_id", "turn_id", "run_id")
        )
        return values if all(values) else None

    def _active_chat_turn_for_conversation(conv_id: str) -> dict:
        with active_chat_turns_lock:
            matches = [
                dict(value)
                for key, value in active_chat_turns.items()
                if key[0] == str(conv_id or "") and isinstance(value, dict)
            ]
        return matches[0] if len(matches) == 1 else {}

    def _desktop_busy_message() -> str:
        return "OSIA is still working in this conversation."

    def _handle_waiting_tool_command(conv_id: str, text: str) -> bool:
        raw = str(text or "").strip()
        if not raw or len(raw) > 160:
            return False
        folded = unicodedata.normalize("NFKD", raw.casefold())
        ascii_text = "".join(
            char for char in folded if not unicodedata.combining(char)
        )
        normalized = re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()
        tokens = tuple(normalized.split())
        if not tokens or len(tokens) > 8:
            return False
        from osia.tool_attempts import get_tool_attempt_coordinator

        coordinator = get_tool_attempt_coordinator()
        if coordinator.store.find_compatible_suspended_turn(conv_id) is None:
            return False
        if normalized in {
            "cancel", "cancel action", "stop action", "annule", "annuler",
            "annule l action", "arrete l action",
        }:
            active = _active_chat_turn_for_conversation(conv_id)
            request_cancel = getattr(active.get("agent"), "request_turn_cancellation", None)
            binding = active.get("binding") if isinstance(active, dict) else None
            if (
                callable(request_cancel)
                and isinstance(binding, dict)
                and all(
                    str(binding.get(key) or "")
                    for key in (
                        "conversation_id", "request_id", "turn_id", "run_id",
                        "provider_id", "model_id", "effective_reasoning",
                    )
                )
            ):
                return bool(request_cancel(str(binding.get("turn_id") or "")))
            return False
        resume_verbs = {
            "continue", "continuer", "relaunch", "relance", "relancer",
            "reprendre", "reprends", "resume",
        }
        resume_suffixes = {
            (), ("action",), ("and", "verify"),
            ("and", "verify", "current", "action"),
            ("and", "verify", "the", "current", "action"),
            ("checking", "current", "action"),
            ("checking", "the", "current", "action"),
            ("et", "verifie"), ("et", "verifier"),
            ("et", "verifie", "l", "action", "en", "cours"),
            ("et", "verifier", "l", "action", "en", "cours"),
            ("la", "verification", "de", "l", "action", "en", "cours"),
        }
        if tokens[0] in resume_verbs and tokens[1:] in resume_suffixes:
            return coordinator.request_reconciliation(conv_id)
        if tokens in {
            ("check",), ("check", "action"),
            ("check", "current", "action"), ("check", "status"),
            ("check", "current", "status"), ("status",),
            ("action", "status"), ("statut",),
            ("verifie", "le", "statut"),
            ("verifier", "le", "statut"),
            ("verifie", "l", "action", "en", "cours"),
            ("verifier", "l", "action", "en", "cours"),
        }:
            active = _active_chat_turn_for_conversation(conv_id)
            binding = active.get("binding") if isinstance(active, dict) else None
            if isinstance(binding, dict):
                bridge.tool_attempt_status.emit(
                    dict(binding), "waiting_tool_result",
                )
            return True
        return False

    def _is_conversation_busy(conv_id: str | None) -> bool:
        if not conv_id:
            return False
        with busy_lock:
            return conv_id in busy_conversations

    def _set_conversation_busy(
        conv_id: str | None,
        busy: bool,
        *,
        owner: dict | None = None,
    ) -> bool:
        if not conv_id:
            return False
        owner_key = _chat_turn_key(owner)
        with busy_lock:
            if busy:
                busy_conversations.add(conv_id)
                if owner_key is not None:
                    busy_turn_keys[conv_id] = owner_key
                    island_lifecycle.begin(("chat", conv_id), owner_key)
                elif conv_id not in busy_turn_keys:
                    busy_turn_keys.pop(conv_id, None)
            else:
                current_owner = busy_turn_keys.get(conv_id)
                if (
                    owner_key is not None
                    and current_owner is not None
                    and current_owner != owner_key
                ):
                    return False
                busy_conversations.discard(conv_id)
                busy_turn_keys.pop(conv_id, None)
                if current_owner is not None:
                    island_lifecycle.finish(("chat", conv_id), current_owner)
        bridge.conversation_busy_changed.emit(conv_id, busy)
        return True

    def _set_chat_island_state(
        binding: dict, state: str, *, terminal: bool = False,
    ) -> None:
        key = _chat_turn_key(binding)
        if key is not None:
            island_lifecycle.update(("chat", key[0]), key, state, terminal=terminal)

    def _project_chat_island_trace(frame: dict) -> None:
        if frame.get("terminal") or frame.get("kind") == "terminal":
            _set_chat_island_state(frame, "idle", terminal=True)
        elif frame.get("kind") in {"action", "waiting_permission"}:
            _set_chat_island_state(frame, "idle")
        elif frame.get("kind") in {
            "goal", "result", "continuation", "reconciliation",
            "public_reasoning_summary",
        }:
            _set_chat_island_state(frame, "thinking")

    def _ensure_active_conversation(seed_text: str | None = None) -> dict:
        nonlocal active_conv
        if active_conv is not None:
            return active_conv
        title = (conversations.smart_title(seed_text)
                 if isinstance(seed_text, str) and seed_text.strip()
                 else None)
        active_conv = conversations.create_conversation(title=title)
        _notify_mobile_conversation_changed(
            active_conv["id"],
            revision=active_conv.get("revision", 0),
            operation_id=active_conv.get("last_operation_id", ""),
        )
        bridge.conversations_changed.emit(active_conv["id"])
        print(f"New conversation: {active_conv['id']}")
        return active_conv

    def _history_for_conversation(conv_id: str) -> list[dict]:
        conv = conversations.get_conversation(conv_id)
        return list((conv or {}).get("messages") or [])

    # â”€â”€ Agent run helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _register_chat_turn(binding: dict, turn_agent) -> None:
        key = _chat_turn_key(binding)
        if key is None:
            return
        with active_chat_turns_lock:
            active_chat_turns[key] = {
                "agent": turn_agent,
                "binding": dict(binding),
            }
        _set_chat_island_state(binding, "thinking")

    def _release_chat_turn(binding: dict, turn_agent) -> None:
        key = _chat_turn_key(binding)
        if key is None:
            return
        with active_chat_turns_lock:
            current = active_chat_turns.get(key)
            if isinstance(current, dict) and current.get("agent") is turn_agent:
                active_chat_turns.pop(key, None)
                island_lifecycle.finish(("chat", key[0]), key)

    def _start_or_run_durable_worker(
        target,
        *,
        name: str,
        allow_inline: bool = True,
    ) -> bool:
        """Never leave a durable Chat holder without its recovery worker."""

        try:
            threading.Thread(
                target=target,
                name=name,
                daemon=True,
            ).start()
            return True
        except Exception as start_error:
            if not allow_inline:
                print(
                    "[OSIA] durable worker thread start deferred: "
                    f"{type(start_error).__name__}"
                )
                retry_delay_ms = [250]

                def _retry_thread_start() -> None:
                    try:
                        threading.Thread(
                            target=target,
                            name=name,
                            daemon=True,
                        ).start()
                        return
                    except Exception as retry_error:
                        print(
                            "[OSIA] durable worker thread retry deferred: "
                            f"{type(retry_error).__name__}"
                        )
                    retry_delay_ms[0] = min(
                        30_000, retry_delay_ms[0] * 2,
                    )
                    QTimer.singleShot(
                        retry_delay_ms[0], _retry_thread_start,
                    )

                try:
                    QTimer.singleShot(
                        retry_delay_ms[0], _retry_thread_start,
                    )
                    return True
                except Exception as timer_error:
                    print(
                        "[OSIA] durable worker retry timer unavailable: "
                        f"{type(timer_error).__name__}"
                    )
                    return False
            print(
                "[OSIA] durable worker thread start failed; running inline: "
                f"{type(start_error).__name__}"
            )
        try:
            target()
            return True
        except Exception as inline_error:
            print(
                "[OSIA] inline durable worker failed closed: "
                f"{type(inline_error).__name__}"
            )
            return False

    def _schedule_desktop_final_persistence_retry(
        turn_agent,
        lifecycle: dict,
        *,
        conv_id: str,
        turn_meta: dict,
        voice_response: bool,
        language_text: str | None,
        user_display_text: str | None,
        tts_session_id: int | None,
        voice_cancelled,
        emit_voice_state,
        continuation_attempt_id: str = "",
    ) -> bool:
        """Retry only the reserved final commit; never replay provider/action work."""

        if lifecycle.get("persistence_retry_scheduled"):
            return True
        turn_id = str(turn_meta.get("turn_id") or "")
        binding = dict(lifecycle.get("binding") or {})
        if not turn_id:
            return False
        lifecycle["persistence_retry_scheduled"] = True
        _set_chat_island_state(binding or turn_meta, "idle", terminal=True)
        lifecycle["durable_wait"] = True
        if turn_agent is not None:
            trace_getter = getattr(turn_agent, "execution_trace_snapshot", None)
            if callable(trace_getter):
                try:
                    from osia.execution_trace import validate_persisted_trace

                    trace = validate_persisted_trace(trace_getter(turn_id))
                except Exception:
                    trace = None
                if isinstance(trace, dict):
                    turn_meta["_execution_trace_v1"] = trace
            try:
                from osia.document_artifacts import normalize_references

                artifact_refs = normalize_references(
                    getattr(turn_agent, "last_run_artifacts", None)
                )
            except Exception:
                artifact_refs = []
            if artifact_refs:
                turn_meta["_artifact_refs"] = artifact_refs

        def _retry() -> None:
            from osia.turn_terminality import load_persisted_terminal_result

            delay = 0.25
            while lifecycle.get("durable_wait"):
                time.sleep(delay)
                persisted = load_persisted_terminal_result(turn_id)
                if not isinstance(persisted, dict):
                    delay = min(30.0, delay * 2.0)
                    continue
                recovered = _persist_desktop_reserved_final({
                    "persisted_terminal": persisted,
                    "turn_meta": turn_meta,
                    "continuation_attempt_id": continuation_attempt_id,
                })
                if not isinstance(recovered, dict):
                    delay = min(30.0, delay * 2.0)
                    continue
                projected_binding = dict(recovered["binding"])
                try:
                    lifecycle["binding"] = dict(projected_binding)
                    if not lifecycle.get("registered"):
                        bridge.chat_turn_started.emit(projected_binding)
                    events = list(
                        (recovered.get("trace") or {}).get("events") or []
                    )
                    if events:
                        bridge.execution_trace.emit({
                            "type": "execution_trace",
                            **dict(events[-1]),
                            "snapshot": events,
                        })
                    display = str(recovered.get("display") or "")
                    bridge.assistant_stream_final.emit(
                        projected_binding, display,
                    )
                    if voice_response and not (
                        voice_cancelled is not None and voice_cancelled()
                    ):
                        context = resolve_turn_context(
                            language_text or user_display_text or display,
                            history=_history_for_conversation(conv_id),
                            saved_language=i18n.get_language(),
                            reference=conv_id,
                        )
                        if emit_voice_state is not None:
                            emit_voice_state("speaking")
                        speak_streaming(
                            clean_response(display),
                            session_id=tts_session_id,
                            language=context.language,
                        )
                except Exception as presentation_error:
                    print(
                        "[OSIA] durable retried final projection failed: "
                        f"{type(presentation_error).__name__}"
                    )
                finally:
                    lifecycle["durable_wait"] = False
                    _release_chat_turn(
                        projected_binding or binding, turn_agent,
                    )
                    if turn_agent is not None:
                        try:
                            turn_agent.clear_tool_attempts()
                        except Exception:
                            pass
                    _set_conversation_busy(
                        conv_id,
                        False,
                        owner=projected_binding or binding,
                    )
                    bridge.conversations_changed.emit(
                        active_conv["id"] if active_conv else ""
                    )
                return

        return _start_or_run_durable_worker(
            _retry,
            name=f"osia-final-persist-{turn_id[-8:]}",
        )

    def _schedule_desktop_terminal_recovery_retry(
        turn_agent,
        lifecycle: dict,
        *,
        user_input,
        conv_id: str,
        history: list[dict],
        turn_meta: dict,
        voice_response: bool,
        user_display_text: str | None,
        language_text: str | None,
        tts_session_id: int | None,
        voice_cancelled,
        emit_voice_state,
        failed_attempt_id: str = "",
        allow_inline_worker: bool = True,
    ) -> bool:
        """Retry reservation plus commit without replaying provider/tool work."""

        if lifecycle.get("terminal_recovery_retry_scheduled"):
            return True
        turn_id = str(turn_meta.get("turn_id") or "")
        if not turn_id:
            return False
        lifecycle["terminal_recovery_retry_scheduled"] = True
        _set_chat_island_state(dict(lifecycle.get("binding") or turn_meta), "idle")
        lifecycle["durable_wait"] = True
        receipt_recorded = [not bool(str(failed_attempt_id or ""))]

        def _retry() -> None:
            delay = 0.25
            while _is_conversation_busy(conv_id):
                time.sleep(delay)
                finalized = False
                try:
                    if not receipt_recorded[0] and turn_agent is not None:
                        recorder = getattr(
                            turn_agent,
                            "record_failed_tool_continuation_receipt",
                            None,
                        )
                        if callable(recorder):
                            receipt_recorded[0] = bool(
                                recorder(str(failed_attempt_id or ""))
                            )
                    finalized = _finalize_failed_desktop_turn(
                        turn_agent,
                        lifecycle,
                        user_input=user_input,
                        conv_id=conv_id,
                        history=history,
                        turn_meta=turn_meta,
                        voice_response=voice_response,
                        user_display_text=user_display_text,
                        language_text=language_text,
                        tts_session_id=tts_session_id,
                        voice_cancelled=voice_cancelled,
                        emit_voice_state=emit_voice_state,
                    )
                except Exception as recovery_error:
                    print(
                        "[OSIA] durable terminal reservation retry failed: "
                        f"{type(recovery_error).__name__}"
                    )
                if not finalized:
                    delay = min(30.0, delay * 2.0)
                    continue
                lifecycle["durable_wait"] = False
                binding = dict(lifecycle.get("binding") or turn_meta)
                if turn_agent is not None:
                    _release_chat_turn(binding, turn_agent)
                    try:
                        turn_agent.clear_tool_attempts()
                    except Exception:
                        pass
                _set_conversation_busy(conv_id, False, owner=binding)
                bridge.conversations_changed.emit(
                    active_conv["id"] if active_conv else ""
                )
                return

        return _start_or_run_durable_worker(
            _retry,
            name=f"osia-terminal-recovery-{turn_id[-8:]}",
            allow_inline=allow_inline_worker,
        )

    def _run_agent_impl(
        turn_agent,
        lifecycle: dict,
        user_input,
        conv_id: str,
        history: list[dict],
        *,
        turn_meta: dict,
        voice_response: bool,
        user_display_text: str | None = None,
        language_text: str | None = None,
        lock_provider: bool = False,
        tts_session_id: int | None = None,
        voice_cancelled=None,
        emit_voice_state=None,
        turn_directives=None,
    ) -> None:
        """Run the agent in the current thread, persist + mirror the result.

        Caller has already marked ``conv_id`` busy and echoed the
        user's text into the popup. ``voice_response`` controls whether
        sentences are streamed to TTS as they arrive.
        """
        nonlocal active_conv
        if lock_provider:
            # Attachment capability was preflighted against this selected path;
            # automatic hierarchy routing must not silently swap it mid-turn.
            turn_agent._disable_hierarchy_for_turn = True
        if voice_response and tts_session_id is None:
            tts_session_id = start_streaming_session()
        settings = load_settings()
        selected_model = settings.get("selected_model")
        selected_provider = settings.get("selected_model_provider") or None
        pinned_provider = str(
            turn_meta.get("_pinned_provider_id") or ""
        ).strip().lower()
        pinned_model = str(turn_meta.get("_pinned_model_id") or "").strip()
        pinned_reasoning = str(
            turn_meta.get("_pinned_effective_reasoning") or ""
        ).strip().lower()
        pinned_route = bool(pinned_provider or pinned_model or pinned_reasoning)
        if pinned_route and not all(
            (pinned_provider, pinned_model, pinned_reasoning)
        ):
            raise RuntimeError("incomplete persisted desktop Chat route")
        if pinned_route:
            from osia.models import PROVIDERS
            from osia.reasoning import resolve_reasoning_option_id

            if pinned_provider not in PROVIDERS:
                raise RuntimeError("persisted desktop Chat provider unavailable")
            turn_agent.set_model(pinned_model, provider=pinned_provider)
            if (
                turn_agent.provider != pinned_provider
                or turn_agent.model != pinned_model
            ):
                raise RuntimeError("persisted desktop Chat route not restored")
            if resolve_reasoning_option_id(
                pinned_provider,
                pinned_model,
                settings={
                    "default": pinned_reasoning,
                    "providers": {},
                    "models": {},
                },
            ) != pinned_reasoning:
                raise RuntimeError("persisted desktop Chat reasoning unavailable")
        elif selected_model and not (
            turn_directives is not None
            and turn_directives.model_override is not None
        ):
            try:
                turn_agent.set_model(selected_model, provider=selected_provider)
            except Exception as e:
                print(f"[DEBUG] set_model({selected_model!r}) failed: {e}")
        if turn_directives is not None:
            from osia.slash_commands import apply_to_agent

            apply_to_agent(turn_agent, turn_directives)
        if pinned_route:
            turn_agent._reasoning_override_for_turn = pinned_reasoning

        authoritative_text = (
            language_text
            if isinstance(language_text, str)
            else user_display_text
            if isinstance(user_display_text, str)
            else user_input
        )
        turn_context = resolve_turn_context(
            authoritative_text,
            history=history,
            saved_language=i18n.get_language(),
            reference=conv_id,
        )
        turn_binding = {
            "conversation_id": conv_id,
            "request_id": str(turn_meta.get("request_id") or ""),
            "turn_id": str(turn_meta.get("turn_id") or ""),
            "run_id": str(
                turn_meta.get("run_id") or turn_meta.get("turn_id") or ""
            ),
            "assistant_message_id": str(
                turn_meta.get("assistant_message_id") or ""
            ),
            "provider_id": str(getattr(turn_agent, "provider", "") or ""),
            "model_id": str(getattr(turn_agent, "model", "") or ""),
            "effective_reasoning": pinned_reasoning,
        }
        if any(
            str(turn_meta.get(key) or "") != str(turn_binding.get(key) or "")
            for key in (
                "conversation_id", "request_id", "turn_id", "run_id",
                "provider_id", "model_id", "effective_reasoning",
            )
        ):
            raise RuntimeError("desktop Chat route changed after acceptance")
        lifecycle["binding"] = dict(turn_binding)
        prev_history_len = len(history)
        suspended_tool_turn = [False]
        canonical_committed = [False]
        canonical_projected = [False]

        def _voice_tts_allowed() -> bool:
            return voice_response and not (
                voice_cancelled is not None and voice_cancelled()
            )

        def on_first_token(*_a, **_kw):
            # Provider text is still a replaceable draft at this point. Keep
            # voice state in thinking until the canonical final is known.
            return None

        def on_stream_sentence(sentence: str) -> None:
            # Sentence-oriented output is not a lossless Markdown transport.
            # The exact canonical final is revealed by the presentation-only
            # UI buffer once terminality has been verified.
            del sentence

        def on_execution_trace(frame: dict) -> None:
            if isinstance(frame, dict) and frame.get("type") == "execution_trace":
                _project_chat_island_trace(frame)
                bridge.execution_trace.emit(dict(frame))
                _notify_mobile_execution_trace(frame)

        def on_tool_attempt_status(status: str, attempt_id: str) -> None:
            del attempt_id
            cancellation_committed = getattr(
                turn_agent, "turn_cancellation_committed", None,
            )
            if callable(cancellation_committed) and cancellation_committed(
                str(turn_meta.get("turn_id") or "")
            ):
                return
            print(f"[OSIA] normal chat tool status: {status}")
            from osia.chat_routing import project_chat_tool_attempt_status

            safe_status = project_chat_tool_attempt_status(status)
            bridge.tool_attempt_status.emit(dict(turn_binding), safe_status)
            # "verifying" merges passive receipt polling and an actual model
            # continuation in Chat. Only the latter owns Thinking here.
            _set_chat_island_state(
                turn_binding,
                "thinking" if status in {
                    "resuming_tool_turn", "blocked_unknown_effect",
                } else "idle",
            )

        def on_tool_attempt_final(
            attempt_id: str,
            final_text: str,
            error,
        ) -> bool:
            canonical_committed = False
            try:
                terminal = _terminality_result(
                    turn_agent,
                    str(turn_meta.get("turn_id") or ""),
                    final_text,
                )
                if not terminal:
                    receipt_recorded = False
                    recorder = getattr(
                        turn_agent,
                        "record_failed_tool_continuation_receipt",
                        None,
                    )
                    if callable(recorder):
                        try:
                            receipt_recorded = bool(recorder(attempt_id))
                        except Exception:
                            receipt_recorded = False
                    try:
                        finalized = _finalize_failed_desktop_turn(
                            turn_agent,
                            lifecycle,
                            user_input=user_input,
                            conv_id=conv_id,
                            history=history,
                            turn_meta=turn_meta,
                            voice_response=voice_response,
                            user_display_text=user_display_text,
                            language_text=language_text,
                            tts_session_id=tts_session_id,
                            voice_cancelled=voice_cancelled,
                            emit_voice_state=emit_voice_state,
                        )
                    except Exception:
                        finalized = False
                    if finalized:
                        canonical_committed = True
                        return True
                    print(
                        "[TERMINALITY] desktop pending continuation retained; "
                        "durable terminal reservation retry scheduled"
                    )
                    return _schedule_desktop_terminal_recovery_retry(
                        turn_agent,
                        lifecycle,
                        user_input=user_input,
                        conv_id=conv_id,
                        history=history,
                        turn_meta=turn_meta,
                        voice_response=voice_response,
                        user_display_text=user_display_text,
                        language_text=language_text,
                        tts_session_id=tts_session_id,
                        voice_cancelled=voice_cancelled,
                        emit_voice_state=emit_voice_state,
                        failed_attempt_id=(
                            "" if receipt_recorded else str(attempt_id or "")
                        ),
                    )
                if terminal.get("terminal_state") == "user_cancelled":
                    canonical_committed = True
                    return True
                if error is not None:
                    final_text = str(terminal.get("final_text") or final_text or "")
                if not _persist_agent_delta(
                    conv_id,
                    turn_agent,
                    prev_history_len,
                    user_display_text=user_display_text,
                    turn_meta=turn_meta,
                ):
                    print(
                        "[TERMINALITY] desktop pending final retained until "
                        "canonical persistence succeeds"
                    )
                    return _schedule_desktop_final_persistence_retry(
                        turn_agent,
                        lifecycle,
                        conv_id=conv_id,
                        turn_meta=turn_meta,
                        voice_response=voice_response,
                        language_text=language_text,
                        user_display_text=user_display_text,
                        tts_session_id=tts_session_id,
                        voice_cancelled=voice_cancelled,
                        emit_voice_state=emit_voice_state,
                    )
                canonical_committed = True
                try:
                    display = strip_tool_artifacts(final_text or "")
                    if error is None:
                        bridge.assistant_stream_final.emit(
                            dict(turn_binding), display,
                        )
                    else:
                        bridge.assistant_stream_error.emit(
                            dict(turn_binding), display,
                        )
                    if _voice_tts_allowed():
                        if emit_voice_state is not None:
                            emit_voice_state("speaking")
                        speak_streaming(
                            clean_response(display),
                            session_id=tts_session_id,
                            language=turn_context.speech_language,
                        )
                except Exception as presentation_error:
                    print(
                        "[OSIA] durable pending final projection failed: "
                        f"{type(presentation_error).__name__}"
                    )
                return True
            finally:
                if canonical_committed:
                    _release_chat_turn(turn_binding, turn_agent)
                    try:
                        turn_agent.clear_tool_attempts()
                    except Exception:
                        pass
                    _set_conversation_busy(
                        conv_id, False, owner=turn_binding,
                    )
                    bridge.conversations_changed.emit(
                        active_conv["id"] if active_conv else ""
                    )

        turn_agent.configure_tool_attempts(
            conversation_id=conv_id,
            turn_id=str(turn_meta.get("turn_id") or ""),
            request_id=str(turn_meta.get("request_id") or ""),
            final_response_message_id=str(
                turn_meta.get("assistant_message_id") or ""
            ),
            status_callback=on_tool_attempt_status,
            final_callback=on_tool_attempt_final,
            on_sentence=on_stream_sentence,
            on_first_token=on_first_token,
            existing_attempt_id=str(
                turn_meta.get("_existing_tool_attempt_id") or ""
            ),
        )
        configure_trace = getattr(turn_agent, "configure_execution_trace", None)
        if callable(configure_trace):
            configure_trace(
                conversation_id=conv_id,
                request_id=str(turn_meta.get("request_id") or ""),
                turn_id=str(turn_meta.get("turn_id") or ""),
                run_id=str(
                    turn_meta.get("run_id") or turn_meta.get("turn_id") or ""
                ),
                provider_id=str(getattr(turn_agent, "provider", "") or ""),
                model_id=str(getattr(turn_agent, "model", "") or ""),
                effective_reasoning=pinned_reasoning,
                callback=on_execution_trace,
            )
            trace_binding = turn_agent.execution_trace_binding(
                str(turn_meta.get("turn_id") or "")
            )
            if isinstance(trace_binding, dict):
                turn_binding.update(trace_binding)
                turn_meta.update({
                    key: str(trace_binding.get(key) or "")
                    for key in (
                        "conversation_id", "request_id", "turn_id", "run_id",
                        "provider_id", "model_id", "effective_reasoning",
                    )
                })
                lifecycle["binding"] = dict(turn_binding)

        configure_intent = getattr(
            turn_agent, "configure_turn_terminality_intent", None,
        )
        if callable(configure_intent):
            configure_intent(
                objective=str(turn_meta.get("_terminality_objective") or ""),
                execution_binding=turn_binding,
            )

        _register_chat_turn(turn_binding, turn_agent)
        lifecycle["registered"] = True
        bridge.chat_turn_started.emit(dict(turn_binding))

        try:
            if voice_response:
                print(
                    "[TTS] main.wake_mode: streaming TTS wired for this turn "
                    "(sentences go to speak_streaming)",
                    flush=True,
                )
            else:
                print(
                    "[TTS] main.wake_mode: TTS disabled for this turn (text-only)",
                    flush=True,
                )
            lifecycle["provider_started"] = True
            reply = _run_turn_to_terminality(
                turn_agent,
                user_input,
                turn_id=str(turn_meta.get("turn_id") or ""),
                semantic_objective=str(
                    turn_meta.get("_terminality_objective") or ""
                ),
                history=history,
                on_sentence=on_stream_sentence,
                on_first_token=on_first_token,
                current_conversation_id=conv_id,
                turn_context=turn_context,
            )
            from osia.tool_attempts import WAITING_TOOL_RESULT

            if reply == WAITING_TOOL_RESULT:
                _set_chat_island_state(turn_binding, "idle")
                if not turn_agent.activate_pending_tool_attempt():
                    raise RuntimeError("pending tool attempt could not be activated")
                suspended_tool_turn[0] = True
                lifecycle["durable_wait"] = True
                return
            terminal = _terminality_result(
                turn_agent,
                str(turn_meta.get("turn_id") or ""),
                reply,
            )
            if not terminal:
                raise RuntimeError(
                    "agent returned without a verified terminal reservation"
                )
            if terminal.get("terminal_state") == "user_cancelled":
                canonical_committed[0] = True
                return
            if voice_response:
                print(
                    f"[TTS] main.wake_mode: agent.run() returned "
                    f"({len(reply or '')} chars) â€” sentences were streamed during the run",
                    flush=True,
                )
            if not _persist_agent_delta(
                conv_id,
                turn_agent,
                prev_history_len,
                user_display_text=user_display_text,
                turn_meta=turn_meta,
            ):
                print(
                    "[TERMINALITY] desktop final retained until canonical "
                    "persistence succeeds"
                )
                _schedule_desktop_final_persistence_retry(
                    turn_agent,
                    lifecycle,
                    conv_id=conv_id,
                    turn_meta=turn_meta,
                    voice_response=voice_response,
                    language_text=language_text,
                    user_display_text=user_display_text,
                    tts_session_id=tts_session_id,
                    voice_cancelled=voice_cancelled,
                    emit_voice_state=emit_voice_state,
                )
                return
            canonical_committed[0] = True
            display = strip_tool_artifacts(reply or "")
            bridge.assistant_stream_final.emit(dict(turn_binding), display)
            canonical_projected[0] = True
            if _voice_tts_allowed():
                if emit_voice_state is not None:
                    emit_voice_state("speaking")
                speak_streaming(
                    clean_response(display),
                    session_id=tts_session_id,
                    language=turn_context.speech_language,
                )
        except Exception as e:
            if canonical_committed[0]:
                if not canonical_projected[0]:
                    display = strip_tool_artifacts(reply or "")
                    bridge.assistant_stream_final.emit(
                        dict(turn_binding), display,
                    )
                    canonical_projected[0] = True
                return
            terminal = _terminality_result(
                turn_agent,
                str(turn_meta.get("turn_id") or ""),
            )
            if not terminal:
                if lifecycle.get("durable_wait"):
                    return
                raise
            if terminal.get("terminal_state") == "user_cancelled":
                canonical_committed[0] = True
                return
            display = strip_tool_artifacts(
                str(terminal.get("final_text") or "")
            )
            if not _persist_agent_delta(
                conv_id,
                turn_agent,
                prev_history_len,
                user_display_text=user_display_text,
                turn_meta=turn_meta,
            ):
                print(
                    "[TERMINALITY] desktop recovered final retained until "
                    "canonical persistence succeeds"
                )
                _schedule_desktop_final_persistence_retry(
                    turn_agent,
                    lifecycle,
                    conv_id=conv_id,
                    turn_meta=turn_meta,
                    voice_response=voice_response,
                    language_text=language_text,
                    user_display_text=user_display_text,
                    tts_session_id=tts_session_id,
                    voice_cancelled=voice_cancelled,
                    emit_voice_state=emit_voice_state,
                )
                return
            bridge.assistant_stream_final.emit(dict(turn_binding), display)
            if _voice_tts_allowed():
                speak_streaming(
                    clean_response(display),
                    session_id=tts_session_id,
                    language=turn_context.speech_language,
                )
            return
        finally:
            if (
                not suspended_tool_turn[0]
                and not lifecycle.get("durable_wait")
            ):
                _release_chat_turn(turn_binding, turn_agent)
                try:
                    turn_agent.clear_tool_attempts()
                except Exception:
                    pass
                _set_conversation_busy(
                    conv_id, False, owner=turn_binding,
                )
                bridge.conversations_changed.emit(
                    active_conv["id"] if active_conv else ""
                )

    def _finalize_failed_desktop_turn(
        turn_agent,
        lifecycle: dict,
        *,
        user_input,
        conv_id: str,
        history: list[dict],
        turn_meta: dict,
        voice_response: bool,
        user_display_text: str | None,
        language_text: str | None,
        tts_session_id: int | None,
        voice_cancelled,
        emit_voice_state,
    ) -> bool:
        """Reserve one safe setup failure or project an existing final receipt."""

        from osia.turn_terminality import (
            TerminalState,
            TurnTerminalityController,
            live_process_identity,
            load_persisted_execution_binding,
            load_persisted_terminal_result,
        )

        turn_id = str(turn_meta.get("turn_id") or "")
        run_id = str(turn_meta.get("run_id") or turn_id)
        objective = str(
            turn_meta.get("_terminality_objective")
            or language_text
            or user_display_text
            or (user_input if isinstance(user_input, str) else "")
        ).strip()
        if not all((turn_id, run_id, objective)):
            return False

        binding = load_persisted_execution_binding(turn_id)
        if binding is None and turn_agent is not None:
            trace_getter = getattr(turn_agent, "execution_trace_binding", None)
            if callable(trace_getter):
                try:
                    binding = trace_getter(turn_id)
                except Exception:
                    binding = None
        if binding is None and turn_agent is not None:
            configure_trace = getattr(turn_agent, "configure_execution_trace", None)
            if callable(configure_trace):
                try:
                    configure_trace(
                        conversation_id=conv_id,
                        request_id=str(turn_meta.get("request_id") or ""),
                        turn_id=turn_id,
                        run_id=run_id,
                        provider_id=str(
                            turn_meta.get("_pinned_provider_id")
                            or getattr(turn_agent, "provider", "")
                            or ""
                        ),
                        model_id=str(
                            turn_meta.get("_pinned_model_id")
                            or getattr(turn_agent, "model", "")
                            or ""
                        ),
                        effective_reasoning=str(
                            turn_meta.get("_pinned_effective_reasoning") or ""
                        ),
                    )
                    binding = turn_agent.execution_trace_binding(turn_id)
                except Exception:
                    binding = None
        expected = {
            "conversation_id": conv_id,
            "request_id": str(turn_meta.get("request_id") or ""),
            "turn_id": turn_id,
            "run_id": run_id,
        }
        if binding is None:
            binding = {
                **expected,
                "provider_id": str(
                    turn_meta.get("provider_id")
                    or turn_meta.get("_pinned_provider_id")
                    or ""
                ),
                "model_id": str(
                    turn_meta.get("model_id")
                    or turn_meta.get("_pinned_model_id")
                    or ""
                ),
                "effective_reasoning": str(
                    turn_meta.get("effective_reasoning")
                    or turn_meta.get("_pinned_effective_reasoning")
                    or ""
                ),
            }
        if not isinstance(binding, dict) or any(
            str(binding.get(key) or "") != value
            for key, value in expected.items()
        ) or any(
            not str(binding.get(key) or "")
            for key in ("provider_id", "model_id", "effective_reasoning")
        ):
            return False

        persisted = load_persisted_terminal_result(turn_id)
        if persisted is None:
            controller = getattr(turn_agent, "_terminality_controller", None)
            if controller is None:
                from osia.legacy_v2_recovery import recover_interrupted_v2_final
                controller = recover_interrupted_v2_final(turn_id, objective, binding)
            if controller is None:
                try:
                    from osia.execution_strategy import derive_semantic_mission
                    from osia.goal_contract import (
                        goal_contract_from_dict,
                        goal_contract_from_semantic_mission,
                    )

                    controller = TurnTerminalityController(
                        turn_id=turn_id,
                        objective=objective,
                        execution_required=True,
                    )
                    controller_snapshot = controller.snapshot()
                    persisted_contract = controller_snapshot.get("goal_contract")
                    contract = (
                        goal_contract_from_dict(persisted_contract)
                        if isinstance(persisted_contract, dict)
                        and persisted_contract else
                        goal_contract_from_semantic_mission(
                            derive_semantic_mission(objective),
                            execution_binding=binding,
                        )
                    )
                    controller.set_execution_binding(binding)
                    controller.set_goal_contract(contract)
                except Exception:
                    return False
            snapshot = controller.snapshot()
            invocations = snapshot.get("invocations") or []
            active_invocations = [
                item for item in invocations
                if isinstance(item, dict) and item.get("status") == "active"
            ]
            if active_invocations:
                owns_controller = bool(
                    turn_agent is not None
                    and controller
                    is getattr(turn_agent, "_terminality_controller", None)
                    and str(getattr(controller, "turn_id", "") or "")
                    == turn_id
                )
                _alive, current_identity = live_process_identity(os.getpid())
                if not owns_controller or any(
                    item.get("owner_pid") not in (None, os.getpid())
                    or (
                        str(item.get("owner_process_identity") or "")
                        and current_identity
                        and str(item.get("owner_process_identity") or "")
                        != current_identity
                    )
                    for item in active_invocations
                ):
                    return False
                for item in active_invocations:
                    invocation_id = str(item.get("invocation_id") or "")
                    if not invocation_id:
                        continue
                    controller.record_provider_invocation_completed(
                        invocation_id,
                        native_status="failed",
                    )
                    controller.record_typed_evidence(
                        outcome="failed_before_effect",
                        evidence="desktop_turn_worker_failed",
                        reason_code="durable_turn_setup_failed",
                    )
                snapshot = controller.snapshot()
            if (
                snapshot.get("active_attempts")
                or snapshot.get("permission_state") == "pending"
                or any(
                    isinstance(item, dict) and item.get("status") == "active"
                    for item in snapshot.get("invocations") or []
                )
            ):
                return False
            controller.set_strategy_frontier(())
            context = resolve_turn_context(
                language_text or user_display_text or objective,
                history=history,
                saved_language=i18n.get_language(),
                reference=conv_id,
            )
            cancellation = snapshot.get("cancellation") or {}
            hard_safety_reason = str(
                snapshot.get("hard_safety_reason") or ""
            )
            if (
                cancellation.get("requested")
                and not cancellation.get("effect_possible")
                and not snapshot.get("unknown_effects")
            ):
                terminal_state = TerminalState.USER_CANCELLED.value
                reason_code = "authoritative_user_cancellation"
                final_text = localized_text(
                    "chat.status_cancelled_proven", context.language,
                )
            elif (
                cancellation.get("requested")
                and snapshot.get("unknown_effects")
            ):
                terminal_state = TerminalState.BLOCKED_VERIFIED.value
                reason_code = "durable_outcome_unknown"
                final_text = localized_text(
                    "chat.status_outcome_unknown", context.language,
                )
            elif hard_safety_reason:
                terminal_state = TerminalState.HARD_SAFETY_BLOCK.value
                reason_code = hard_safety_reason
                final_text = (
                    "Cette action est bloquÃ©e par une barriÃ¨re de sÃ©curitÃ© "
                    "absolue."
                    if context.language == "fr" else
                    "This action is blocked by an absolute safety barrier."
                )
            elif snapshot.get("unknown_effects"):
                terminal_state = TerminalState.BLOCKED_VERIFIED.value
                reason_code = "durable_outcome_unknown"
                final_text = localized_text(
                    "chat.status_outcome_unknown", context.language,
                )
            else:
                terminal_state = TerminalState.BLOCKED_VERIFIED.value
                reason_code = "durable_turn_setup_failed"
                final_text = localized_text(
                    "provider_failure" if invocations else "desktop_turn_setup_failed",
                    context.language,
                )
            controller.reserve_final(
                terminal_state,
                reason_code=reason_code,
                provider_route=(
                    f"{binding['provider_id']}:{binding['model_id']}"
                ),
                final_text=final_text,
            )
            persisted = load_persisted_terminal_result(turn_id)
        if not isinstance(persisted, dict):
            return False

        recovered = _persist_desktop_reserved_final({
            "persisted_terminal": persisted,
            "turn_meta": turn_meta,
        })
        if not isinstance(recovered, dict):
            return False
        projected_binding = dict(recovered["binding"])
        bridge.chat_turn_started.emit(projected_binding)
        events = list((recovered.get("trace") or {}).get("events") or [])
        if events:
            bridge.execution_trace.emit({
                "type": "execution_trace",
                **dict(events[-1]),
                "snapshot": events,
            })
        display = str(recovered.get("display") or "")
        bridge.assistant_stream_final.emit(projected_binding, display)
        if voice_response and not (
            voice_cancelled is not None and voice_cancelled()
        ):
            context = resolve_turn_context(
                language_text or user_display_text or objective,
                history=history,
                saved_language=i18n.get_language(),
                reference=conv_id,
            )
            if emit_voice_state is not None:
                emit_voice_state("speaking")
            speak_streaming(
                clean_response(display),
                session_id=tts_session_id,
                language=context.language,
            )
        return True

    def _finalize_desktop_worker_start_failure(
        *,
        user_input,
        conv_id: str,
        history: list[dict],
        turn_meta: dict,
        user_display_text: str | None,
        language_text: str | None,
    ) -> None:
        """Close one accepted desktop turn when its provider worker cannot start."""

        lifecycle = {
            "binding": {
                key: str(
                    turn_meta.get(key)
                    or turn_meta.get("_pinned_" + key)
                    or ""
                )
                for key in (
                    "conversation_id", "request_id", "turn_id", "run_id",
                    "provider_id", "model_id", "effective_reasoning",
                )
            },
            "registered": False,
            "provider_started": False,
            "durable_wait": False,
        }
        try:
            finalized = _finalize_failed_desktop_turn(
                None,
                lifecycle,
                user_input=user_input,
                conv_id=conv_id,
                history=history,
                turn_meta=turn_meta,
                voice_response=False,
                user_display_text=user_display_text,
                language_text=language_text,
                tts_session_id=None,
                voice_cancelled=None,
                emit_voice_state=None,
            )
        except Exception as recovery_error:
            print(
                "[OSIA] desktop worker-start finalization deferred: "
                f"{type(recovery_error).__name__}"
            )
            finalized = False
        if not finalized:
            if _schedule_desktop_terminal_recovery_retry(
                None,
                lifecycle,
                user_input=user_input,
                conv_id=conv_id,
                history=history,
                turn_meta=turn_meta,
                voice_response=False,
                user_display_text=user_display_text,
                language_text=language_text,
                tts_session_id=None,
                voice_cancelled=None,
                emit_voice_state=None,
                allow_inline_worker=False,
            ):
                return
            print(
                "[OSIA] desktop worker-start failure remains durably recoverable"
            )
            return
        _set_conversation_busy(conv_id, False, owner=turn_meta)
        bridge.conversations_changed.emit(
            active_conv["id"] if active_conv else ""
        )

    def _run_agent(
        user_input,
        conv_id: str,
        history: list[dict],
        *,
        turn_meta: dict,
        voice_response: bool,
        user_display_text: str | None = None,
        language_text: str | None = None,
        lock_provider: bool = False,
        tts_session_id: int | None = None,
        voice_cancelled=None,
        emit_voice_state=None,
        turn_directives=None,
    ) -> None:
        lifecycle = {
            "binding": {},
            "registered": False,
            "provider_started": False,
            "durable_wait": False,
        }
        turn_agent = None
        try:
            turn_agent = OSIAAgent()
            _run_agent_impl(
                turn_agent,
                lifecycle,
                user_input,
                conv_id,
                history,
                turn_meta=turn_meta,
                voice_response=voice_response,
                user_display_text=user_display_text,
                language_text=language_text,
                lock_provider=lock_provider,
                tts_session_id=tts_session_id,
                voice_cancelled=voice_cancelled,
                emit_voice_state=emit_voice_state,
                turn_directives=turn_directives,
            )
            return
        except Exception as exc:
            print(
                "[OSIA] desktop Chat lifecycle failed closed: "
                f"{type(exc).__name__}"
            )
            finalized = False
            try:
                finalized = _finalize_failed_desktop_turn(
                    turn_agent,
                    lifecycle,
                    user_input=user_input,
                    conv_id=conv_id,
                    history=history,
                    turn_meta=turn_meta,
                    voice_response=voice_response,
                    user_display_text=user_display_text,
                    language_text=language_text,
                    tts_session_id=tts_session_id,
                    voice_cancelled=voice_cancelled,
                    emit_voice_state=emit_voice_state,
                )
            except Exception as recovery_exc:
                print(
                    "[OSIA] desktop Chat terminal recovery failed closed: "
                    f"{type(recovery_exc).__name__}"
                )
            if not finalized:
                _schedule_desktop_terminal_recovery_retry(
                    turn_agent,
                    lifecycle,
                    user_input=user_input,
                    conv_id=conv_id,
                    history=history,
                    turn_meta=turn_meta,
                    voice_response=voice_response,
                    user_display_text=user_display_text,
                    language_text=language_text,
                    tts_session_id=tts_session_id,
                    voice_cancelled=voice_cancelled,
                    emit_voice_state=emit_voice_state,
                )
                return
        binding = dict(lifecycle.get("binding") or {})
        if turn_agent is not None:
            _release_chat_turn(binding, turn_agent)
            try:
                turn_agent.clear_tool_attempts()
            except Exception:
                pass
        _set_conversation_busy(conv_id, False, owner=binding or turn_meta)
        bridge.conversations_changed.emit(active_conv["id"] if active_conv else "")

    def _restore_incomplete_desktop_chat_turns() -> None:
        """Reattach exact durable Chat state; never replay by recency."""

        try:
            candidates = _incomplete_desktop_chat_turns()
        except Exception as exc:
            print(
                "[OSIA] desktop Chat recovery inventory unavailable: "
                f"{type(exc).__name__}"
            )
            return
        for candidate in candidates:
            conv_id = str(candidate.get("conversation_id") or "")
            turn_meta = dict(candidate.get("turn_meta") or {})
            if not conv_id or _is_conversation_busy(conv_id):
                continue
            _set_conversation_busy(conv_id, True, owner=turn_meta)

            if candidate.get("needs_terminal_reservation") is True:
                lifecycle = {
                    "binding": dict(turn_meta),
                    "registered": False,
                    "provider_started": False,
                    "durable_wait": True,
                }
                _schedule_desktop_terminal_recovery_retry(
                    None,
                    lifecycle,
                    user_input=str(candidate.get("objective") or ""),
                    conv_id=conv_id,
                    history=list(candidate.get("history") or []),
                    turn_meta=turn_meta,
                    voice_response=False,
                    user_display_text=str(candidate.get("objective") or ""),
                    language_text=str(candidate.get("objective") or ""),
                    tts_session_id=None,
                    voice_cancelled=None,
                    emit_voice_state=None,
                    allow_inline_worker=False,
                )
                continue

            if isinstance(candidate.get("persisted_terminal"), dict):
                def _project_reserved(
                    exact_candidate=dict(candidate), exact_conv_id=conv_id,
                ) -> None:
                    durable_retry_scheduled = False
                    try:
                        recovered = _persist_desktop_reserved_final(exact_candidate)
                        if not isinstance(recovered, dict):
                            recovery_lifecycle = {
                                "binding": dict(
                                    exact_candidate.get("turn_meta") or {}
                                ),
                                "registered": False,
                                "provider_started": False,
                                "durable_wait": True,
                            }
                            durable_retry_scheduled = (
                                _schedule_desktop_final_persistence_retry(
                                    None,
                                    recovery_lifecycle,
                                    conv_id=exact_conv_id,
                                    turn_meta=dict(
                                        exact_candidate.get("turn_meta") or {}
                                    ),
                                    voice_response=False,
                                    language_text=str(
                                        exact_candidate.get("objective") or ""
                                    ),
                                    user_display_text=str(
                                        exact_candidate.get("objective") or ""
                                    ),
                                    tts_session_id=None,
                                    voice_cancelled=None,
                                    emit_voice_state=None,
                                    continuation_attempt_id=str(
                                        exact_candidate.get(
                                            "continuation_attempt_id"
                                        ) or ""
                                    ),
                                )
                            )
                            return
                        binding = dict(recovered["binding"])
                        bridge.chat_turn_started.emit(binding)
                        events = list(
                            (recovered.get("trace") or {}).get("events") or []
                        )
                        if events:
                            bridge.execution_trace.emit({
                                "type": "execution_trace",
                                **dict(events[-1]),
                                "snapshot": events,
                            })
                        bridge.assistant_stream_final.emit(
                            binding, str(recovered.get("display") or ""),
                        )
                    finally:
                        if not durable_retry_scheduled:
                            _set_conversation_busy(
                                exact_conv_id,
                                False,
                                owner=exact_candidate.get("turn_meta") or {},
                            )
                            bridge.conversations_changed.emit(
                                active_conv["id"] if active_conv else ""
                            )

                _start_or_run_durable_worker(
                    _project_reserved,
                    name="osia-desktop-final-recovery",
                    allow_inline=False,
                )
                continue

            def _resume(
                exact_candidate=dict(candidate),
                exact_conv_id=conv_id,
                exact_turn_meta=turn_meta,
            ) -> None:
                _run_agent(
                    str(exact_candidate.get("objective") or ""),
                    exact_conv_id,
                    list(exact_candidate.get("history") or []),
                    turn_meta=exact_turn_meta,
                    voice_response=False,
                    user_display_text=str(
                        exact_candidate.get("objective") or ""
                    ),
                    language_text=str(
                        exact_candidate.get("objective") or ""
                    ),
                    turn_directives=exact_candidate.get("turn_directives"),
                )

            try:
                threading.Thread(
                    target=_resume,
                    name="osia-desktop-chat-recovery",
                    daemon=True,
                ).start()
            except Exception as start_error:
                print(
                    "[OSIA] desktop recovery worker could not start: "
                    f"{type(start_error).__name__}"
                )
                _finalize_desktop_worker_start_failure(
                    user_input=str(candidate.get("objective") or ""),
                    conv_id=conv_id,
                    history=list(candidate.get("history") or []),
                    turn_meta=turn_meta,
                    user_display_text=str(candidate.get("objective") or ""),
                    language_text=str(candidate.get("objective") or ""),
                )

    # â”€â”€ Manual activation handler (voice path) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def handle_wake(generation: int):
        if not wake_gate.consume(
            generation,
            is_current=manual_activation_open.is_set() and generation == 0,
        ):
            print("âš ï¸  Stale or unreserved wake event â€” ignored.")
            return

        voice_session = object()
        if not voice_ui_lifecycle.begin(voice_session):
            wake_gate.finish()
            return
        island_lifecycle.begin("voice", voice_session)
        owned_tts_session = [None]

        def _emit_voice_state(state: str) -> bool:
            return voice_ui_lifecycle.emit_if_current(
                voice_session,
                # Model activity is owned by the correlated Chat turn. Voice
                # preparation/transcription alone does not prove reasoning.
                lambda: island_lifecycle.update(
                    "voice", voice_session, "idle" if state == "thinking" else state,
                ),
            )

        _emit_voice_state("thinking")

        def _finish_wake_turn() -> None:
            def _wait_for_owned_playback() -> bool:
                if owned_tts_session[0] is None:
                    return True
                return wait_for_playback_complete(
                    session_id=owned_tts_session[0]
                )

            def _emit_post_voice_state() -> None:
                island_lifecycle.finish("voice", voice_session)

            voice_ui_lifecycle.finalize(
                voice_session,
                wait_for_playback=_wait_for_owned_playback,
                release_gate=wake_gate.finish,
                emit_final_state=_emit_post_voice_state,
            )

        # Bloc 4 â€” Live API voice-to-voice. When enabled, OSIA's local STT
        # (Whisper) transcription and TTS speak steps are bypassed entirely:
        # the provider's live/realtime endpoint streams audio in and speaks
        # back natively. The real WebSocket streaming is a separate task â€”
        # _connect_live_api() is currently a stub (see osia.voice.live_api).
        try:
            live_settings = load_settings()
        except Exception as exc:
            print(f"[wake] could not load voice settings: {exc}")
            _finish_wake_turn()
            return
        if live_settings.get("liveapi_enabled") and not LIVEAPI_AVAILABLE:
            print(
                "[LIVE API] configured but not yet available; "
                "using local STT/TTS instead.",
                flush=True,
            )
        if live_settings.get("liveapi_enabled") and LIVEAPI_AVAILABLE:
            provider = live_settings.get("liveapi_provider", "google") or "google"
            model = live_settings.get("liveapi_model")
            print(
                f"[LIVE API] active â€” STT and TTS bypassed, using {provider} "
                "live endpoint"
            )

            def live_process():
                try:
                    if voice_shutdown_requested.is_set():
                        return
                    from osia.voice.live_api import _connect_live_api
                    _connect_live_api(provider, model)
                finally:
                    _finish_wake_turn()

            _start_voice_worker(
                live_process,
                name="osia-live-voice",
                on_start_failure=_finish_wake_turn,
            )
            return

        if voice_shutdown_requested.is_set():
            _finish_wake_turn()
            return
        voice_tts_session = start_tts_session(reopen_after_shutdown=False)
        owned_tts_session[0] = voice_tts_session

        def process():
            try:
                if voice_shutdown_requested.is_set():
                    return
                # Deferred heavy import (faster_whisper, ~4.3s on Windows). This
                # runs on the wake daemon thread, never on the GUI startup path.
                from osia.voice.stt import listen_for_command
                if voice_shutdown_requested.is_set():
                    return
                # Optional spoken greeting before listening. When enabled, OSIA
                # speaks the preconfigured text first, then starts capturing the
                # command (speak() is synchronous, so the greeting finishes
                # before the mic opens â€” it never bleeds into the recording).
                if live_settings.get("wake_greeting_enabled"):
                    greeting = (live_settings.get("wake_greeting_text") or "").strip()
                    if greeting:
                        _emit_voice_state("speaking")
                        speak_greeting_cached(
                            greeting, session_id=voice_tts_session,
                        )
                if voice_shutdown_requested.is_set():
                    return

                def _mark_listening_ready() -> None:
                    if not voice_shutdown_requested.is_set():
                        _emit_voice_state("listening")

                user_text = listen_for_command(
                    max_retries=3,
                    on_ready=_mark_listening_ready,
                )
                if voice_shutdown_requested.is_set():
                    return
                if not user_text:
                    fallback_history = (
                        _history_for_conversation(active_conv["id"])
                        if active_conv is not None else []
                    )
                    fallback_context = resolve_turn_context(
                        "",
                        history=fallback_history,
                        saved_language=i18n.get_language(),
                        reference=(active_conv or {}).get("id"),
                    )
                    _emit_voice_state("speaking")
                    speak(
                        localized_text(
                            "stt_not_understood", fallback_context.language,
                        ),
                        session_id=voice_tts_session,
                        language=fallback_context.language,
                    )
                    return
                print(f"[STT] accepted voice input ({len(user_text)} chars).")
                # WOR-10 â€” intercept "nouveau chat" / "new chat" before the LLM.
                if is_new_chat_command(user_text):
                    print(
                        "[voice] local command accepted "
                        "(action=new_conversation)."
                    )
                    bridge.new_chat.emit()        # reset agent + GUI (Qt thread)
                    _notify_mobile_new_chat()      # push {"type":"new_chat"} to mobile
                    _emit_voice_state("speaking")
                    speak(
                        i18n.t("conversations.new_chat_confirm"),
                        session_id=voice_tts_session,
                    )
                    return
                conv = _ensure_active_conversation(user_text)
                conv_id = conv["id"]
                if _is_conversation_busy(conv_id):
                    _handle_waiting_tool_command(conv_id, user_text)
                    _emit_voice_state("speaking")
                    speak(
                        _desktop_busy_message(),
                        session_id=voice_tts_session,
                    )
                    return
                history = _history_for_conversation(conv_id)
                try:
                    turn_meta = _persist_desktop_user_turn(
                        conv_id,
                        user_text,
                        use_task_hierarchy=True,
                    )
                except Exception as exc:
                    print(
                        "[OSIA] desktop voice turn rejected before dispatch: "
                        f"{type(exc).__name__}"
                    )
                    return
                _set_conversation_busy(conv_id, True, owner=turn_meta)
                _emit_voice_state("thinking")
                bridge.chat_message_for_conversation.emit(conv_id, "user", user_text)
                _run_agent(user_text, conv_id, history,
                    turn_meta=turn_meta,
                    voice_response=True,
                    tts_session_id=voice_tts_session,
                    voice_cancelled=voice_shutdown_requested.is_set,
                    emit_voice_state=_emit_voice_state,
                )
            finally:
                _finish_wake_turn()

        _start_voice_worker(
            process,
            name="osia-voice-turn",
            on_start_failure=_finish_wake_turn,
        )

    # â”€â”€ Text handler (typed message in popup) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _send_user_input(
        user_input,
        display_text: str,
        *,
        attachment_turn: bool = False,
        language_text: str | None = None,
        turn_directives=None,
    ) -> bool:
        from osia.slash_commands import SlashDirectiveError

        conv = _ensure_active_conversation(display_text)
        conv_id = conv["id"]
        if attachment_turn:
            popup.bind_unbound_draft(conv_id)
        if _is_conversation_busy(conv_id):
            _handle_waiting_tool_command(conv_id, display_text)
            popup.show_conversation_busy_notice()
            print("Send fired while this conversation is busy; rejected.")
            return False
        tts_stop()
        history = _history_for_conversation(conv_id)
        try:
            persistence_metadata = {}
            if attachment_turn:
                from osia.image_inputs import image_references

                attachment_metadata = [
                    {"category": "image", **reference}
                    for reference in image_references(user_input)
                ]
            else:
                attachment_metadata = []
            if attachment_metadata:
                persistence_metadata["_attachments"] = [
                    dict(item) for item in attachment_metadata
                    if isinstance(item, dict)
                ]
            if turn_directives is not None:
                from osia.slash_commands import validate_live

                turn_directives.require_sendable(
                    body=display_text,
                    has_attachment=attachment_turn,
                )
                validate_live(
                    turn_directives,
                    current_conversation_id=conv_id,
                    has_attachment=attachment_turn,
                )
                persistence_metadata["_turn_directives_v1"] = (
                    turn_directives.to_wire()
                )
            turn_meta = _persist_desktop_user_turn(
                conv_id,
                display_text,
                metadata=persistence_metadata or None,
                turn_directives=turn_directives,
                use_task_hierarchy=not attachment_turn,
            )
        except Exception as exc:
            if isinstance(exc, SlashDirectiveError):
                popup._chat.show_notice(exc.message(i18n.get_language()))
            print(
                "[OSIA] desktop text turn rejected before dispatch: "
                f"{type(exc).__name__}"
            )
            return False
        _set_conversation_busy(conv_id, True, owner=turn_meta)
        bridge.chat_message_for_conversation.emit(conv_id, "user", display_text)

        def process():
            _run_agent(
                user_input,
                conv_id,
                history,
                turn_meta=turn_meta,
                voice_response=False,
                user_display_text=display_text,
                language_text=language_text,
                lock_provider=attachment_turn,
                turn_directives=turn_directives,
            )

        try:
            threading.Thread(target=process, daemon=True).start()
        except Exception as start_error:
            print(
                "[OSIA] desktop Chat worker could not start: "
                f"{type(start_error).__name__}"
            )
            _finalize_desktop_worker_start_failure(
                user_input=user_input,
                conv_id=conv_id,
                history=history,
                turn_meta=turn_meta,
                user_display_text=display_text,
                language_text=language_text,
            )
        return True

    def handle_send_text(text: str):
        _send_user_input(text, text)

    # â”€â”€ Popup chat / message wiring â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def handle_chat_message(role: str, text: str):
        popup.append_message(role, text)

    def handle_chat_message_for_conversation(conv_id: str, role: str, text: str):
        if active_conv and active_conv.get("id") == conv_id:
            popup.append_message(role, text)

    def handle_chat_turn_started(binding: dict):
        popup.begin_chat_turn(binding)

    def handle_assistant_stream_chunk(binding: dict, text: str):
        conv_id = str((binding or {}).get("conversation_id") or "")
        if active_conv and active_conv.get("id") == conv_id:
            popup.append_assistant_stream_chunk(text, binding)

    def handle_assistant_stream_final(binding: dict, text: str):
        _set_chat_island_state(binding, "idle", terminal=True)
        conv_id = str((binding or {}).get("conversation_id") or "")
        if active_conv and active_conv.get("id") == conv_id:
            from osia.chat_screenshots import message_screenshots

            screenshots = message_screenshots(
                conversations.get_conversation(conv_id), str(binding.get("turn_id") or ""),
            )
            if screenshots:
                binding = {**binding, "screenshots": screenshots}
            popup.finalize_assistant_stream(text, binding)

    def handle_assistant_stream_error(binding: dict, text: str):
        _set_chat_island_state(binding, "idle", terminal=True)
        conv_id = str((binding or {}).get("conversation_id") or "")
        if active_conv and active_conv.get("id") == conv_id:
            popup.fail_assistant_stream(text, binding)

    def handle_conversation_busy_changed(conv_id: str, busy: bool):
        popup.set_conversation_busy(conv_id, busy)

    def handle_tool_attempt_status(binding: dict, status: str):
        popup.set_conversation_tool_status_for_turn(binding, status)

    def handle_island_state():
        # Re-read at delivery: Qt may deliver a queued notification after its
        # owner finished or after another conversation/voice session started.
        island.set_state(island_lifecycle.current_state())

    def handle_execution_trace(payload: dict):
        popup.update_chat_execution_activity(payload)

    # â”€â”€ Conversation management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _begin_conversation_delete_projection(
        request_id: str,
        conv_ids: set[str],
        *,
        action: str,
    ) -> dict:
        nonlocal active_conv
        selected = (
            active_conv
            if active_conv and str(active_conv.get("id") or "") in conv_ids
            else None
        )
        context = {
            "action": action,
            "conv_ids": set(conv_ids),
            "active_conv": copy.deepcopy(selected) if selected else None,
            "agent_messages": copy.deepcopy(agent.messages) if selected else None,
            "selection_epoch": selection_epoch[0],
            "snapshot": popup.conversation_snapshot(),
            "visible_ids": popup.visible_conversation_ids(),
        }
        pending_delete_contexts[request_id] = context
        pending_delete_ids.update(conv_ids)
        popup.set_optimistic_conversation_deletions(conv_ids, True)
        if selected:
            tts_stop()
            next_conv = None
            for candidate_id in _deletion_fallback_ids(
                [{"id": conv_id} for conv_id in context["visible_ids"]],
                str(selected.get("id") or ""),
                pending_delete_ids,
            ):
                next_conv = conversations.get_conversation(candidate_id)
                if next_conv is not None:
                    break
            if next_conv is not None:
                _activate_conversation(next_conv)
            else:
                selection_epoch[0] += 1
                agent.reset()
                active_conv = None
                popup.refresh_conversations(None)
                popup.set_messages([])
            context["selection_epoch"] = selection_epoch[0]
        print(
            "[OSIA DeleteTrace] origin=PC phase=ui_removed "
            f"request_id={request_id!r} action={action} "
            f"conversation_ids={sorted(conv_ids)!r} "
            f"ui_removed_at_unix_ns={time.time_ns()}",
            flush=True,
        )
        return context

    def _start_conversation_delete_worker(
        request_id: str,
        conv_ids: set[str],
        *,
        expected_count: int | None = None,
        operation_id: str = "",
    ) -> None:
        def worker() -> None:
            for conv_id in sorted(conv_ids):
                print(
                    "[OSIA DeleteTrace] origin=PC phase=canonical_start "
                    f"conversation_id={conv_id!r} request_id={request_id!r} "
                    f"operation_id={operation_id!r} "
                    f"persistence_location={str(conversations.CONVERSATIONS_DIR)!r} "
                    f"started_at_unix_ns={time.time_ns()}",
                    flush=True,
                )
            result = {
                "request_id": request_id,
                "action": "bulk" if expected_count is not None else "single",
                "success": False,
                "deleted_ids": [],
                "revision": 0,
                "operation_id": operation_id,
            }
            try:
                if expected_count is None:
                    conv_id = next(iter(conv_ids), "")
                    deletion = conversations.delete_conversation_with_receipt(
                        conv_id,
                        operation_id=operation_id,
                        cancel_active_turn=True,
                    )
                    result.update(deletion)
                    result["success"] = bool(deletion.get("success"))
                    result["deleted_ids"] = list(
                        deletion.get("deleted_ids") or []
                    )
                    result["revision"] = int(
                        deletion.get("snapshot_revision")
                        or deletion.get("revision")
                        or 0
                    )
                    result["operation_id"] = str(
                        deletion.get("operation_id") or operation_id
                    )
                else:
                    deletion = conversations.delete_confirmed_ungrouped_conversations(
                        expected_count,
                        operation_id=operation_id,
                    )
                    result.update(deletion)
                    result["success"] = bool(deletion.get("success"))
                    result["deleted_ids"] = list(
                        deletion.get("deleted_ids") or []
                    )
                    result["revision"] = int(deletion.get("revision") or 0)
                    result["operation_id"] = str(
                        deletion.get("operation_id") or operation_id
                    )
            except Exception as exc:
                result["error"] = type(exc).__name__
                print(
                    "[OSIA] Conversation deletion failed: "
                    f"{type(exc).__name__}",
                    flush=True,
                )
            for conv_id in sorted(result.get("deleted_ids") or conv_ids):
                print(
                    "[OSIA DeleteTrace] origin=PC phase=canonical_result "
                    f"conversation_id={conv_id!r} request_id={request_id!r} "
                    f"operation_id={result.get('operation_id')!r} "
                    f"success={bool(result.get('success'))} "
                    f"snapshot_revision={int(result.get('revision') or 0)} "
                    f"persistence_location={str(conversations.CONVERSATIONS_DIR)!r} "
                    f"completed_at_unix_ns={time.time_ns()}",
                    flush=True,
                )
            try:
                bridge.conversation_delete_finished.emit(result)
            except RuntimeError:
                pass

        operation = "bulk" if expected_count is not None else "single"
        thread = threading.Thread(
            target=worker,
            name=f"osia-conversation-delete-{operation}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            bridge.conversation_delete_finished.emit({
                "request_id": request_id,
                "action": operation,
                "success": False,
                "deleted_ids": [],
                "operation_id": operation_id,
                "error": type(exc).__name__,
            })

    def handle_conversation_delete_finished(result: dict) -> None:
        nonlocal active_conv
        request_id = str((result or {}).get("request_id") or "")
        context = pending_delete_contexts.pop(request_id, None)
        if not context:
            return
        conv_ids = set(context.get("conv_ids") or ())
        pending_delete_ids.difference_update(conv_ids)

        if context.get("action") == "single":
            for conv_id in conv_ids:
                active = _active_chat_turn_for_conversation(conv_id)
                binding = active.get("binding")
                if isinstance(binding, dict) and (
                    conversations.was_cancelled_for_conversation_delete(binding)
                ):
                    handle_chat_execution_cancel(binding, discard_stream=False)
                elif result.get("success") and not active:
                    _set_conversation_busy(conv_id, False)

        if not result.get("success"):
            try:
                rollback_snapshot = conversations.conversation_snapshot()
            except Exception:
                rollback_snapshot = context.get("snapshot")
            popup.set_optimistic_conversation_deletions(
                conv_ids,
                False,
                snapshot=rollback_snapshot,
            )
            active_before = context.get("active_conv")
            if (
                active_before
                and selection_epoch[0] == context.get("selection_epoch")
                and active_conv is None
            ):
                active_id = str(active_before.get("id") or "")
                restored = conversations.get_conversation(active_id)
                if restored is None and any(
                    str(item.get("id") or "") == active_id
                    for item in (rollback_snapshot or {}).get("items") or []
                ):
                    restored = copy.deepcopy(active_before)
                if restored is not None:
                    active_conv = restored
                    agent.reset()
                    agent.messages = copy.deepcopy(
                        context.get("agent_messages") or []
                    )
                    popup.refresh_conversations(active_id)
                    popup.set_messages(restored.get("messages") or [])
            error = str(result.get("error") or "")
            if error == "conversation_busy":
                popup.show_conversation_delete_busy(
                    count=max(1, len(result.get("busy_ids") or conv_ids)),
                    bulk=context.get("action") == "bulk",
                )
            else:
                popup.show_conversation_delete_failed(error)
            popup.refresh_conversations(
                active_conv["id"] if active_conv else None
            )
            print(
                "[OSIA] Conversation deletion rejected: "
                f"{result.get('error') or 'canonical_store_rejected'}",
                flush=True,
            )
            return

        popup.set_optimistic_conversation_deletions(
            conv_ids, False, revision=int(result.get("revision") or 0),
        )
        deleted_ids = {
            str(conv_id or "")
            for conv_id in result.get("deleted_ids") or ()
            if str(conv_id or "")
        }
        for conv_id in deleted_ids:
            popup.discard_conversation_draft(conv_id)
        if context.get("action") == "bulk":
            _notify_mobile_conversation_changed(
                "",
                revision=result.get("revision", 0),
                operation_id=result.get("operation_id", ""),
                action="bulk_delete",
                deleted_ids=sorted(deleted_ids),
            )
        else:
            for conv_id in deleted_ids:
                _notify_mobile_conversation_changed(
                    conv_id,
                    revision=result.get("revision", 0),
                    operation_id=result.get("operation_id", ""),
                    action="delete",
                )
        # The row was removed synchronously. The canonical receipt only
        # advances the local revision; polling handles unrelated store edits.

    def handle_new_conversation():
        nonlocal active_conv
        selection_epoch[0] += 1
        tts_stop()
        agent.reset()
        active_conv = conversations.create_conversation()
        _notify_mobile_conversation_changed(
            active_conv["id"],
            revision=active_conv.get("revision", 0),
            operation_id=active_conv.get("last_operation_id", ""),
        )
        popup.refresh_conversations(active_conv["id"])
        popup.set_messages([])
        print(f"ðŸ†• New conversation: {active_conv['id']}")

    def handle_voice_new_chat():
        # GUI-thread reset for the "nouveau chat" voice command (WOR-10).
        # Same effect as clicking "Nouveau chat": clear agent history, start a
        # fresh conversation, and reset the chat view. The voice-session
        # finalizer remains the sole owner of the Dynamic Island ready state.
        nonlocal active_conv
        selection_epoch[0] += 1
        agent.reset()
        active_conv = conversations.create_conversation()
        _notify_mobile_conversation_changed(
            active_conv["id"],
            revision=active_conv.get("revision", 0),
            operation_id=active_conv.get("last_operation_id", ""),
        )
        popup.refresh_conversations(active_conv["id"])
        popup.reset_conversation()
        print(f"ðŸ†• New conversation (voice): {active_conv['id']}")

    def _activate_conversation(conv: dict) -> None:
        nonlocal active_conv
        selection_epoch[0] += 1
        tts_stop()
        agent.reset()
        msgs = conv.get("messages") or []
        # Seed the agent with history so the next turn keeps context.
        agent.messages = [agent.messages[0]] + [
            {
                "role": m["role"],
                "content": m.get("content", ""),
                **{key: m[key] for key in ("_conversation_id", "_turn_id", "_screenshots", "_observed_image")
                   if key in m},
                **(
                    {"_attachments": list(m["_attachments"])}
                    if isinstance(m.get("_attachments"), list) else {}
                ),
            }
            for m in msgs
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        active_conv = conv
        popup.refresh_conversations(active_conv["id"])
        popup.set_messages(msgs)

    def handle_open_conversation(conv_id: str):
        conv = conversations.get_conversation(conv_id)
        if not conv:
            return
        _activate_conversation(conv)
        print(f"ðŸ“‚ Loaded conversation: {conv_id}")

    def handle_delete_conversation(conv_id: str):
        nonlocal active_conv
        if str(conv_id) in pending_delete_ids:
            popup.show_conversation_delete_in_progress()
            return
        cached_items = popup.conversation_snapshot().get("items") or []
        if not any(str(item.get("id") or "") == str(conv_id) for item in cached_items):
            popup.show_conversation_delete_failed("conversation_not_found")
            popup.reload_conversations()
            return
        request_id = f"desktop_delete_{uuid.uuid4().hex}"
        operation_id = f"desktop_delete_{uuid.uuid4().hex}"
        conv_ids = {str(conv_id)}
        _begin_conversation_delete_projection(
            request_id, conv_ids, action="single",
        )
        _start_conversation_delete_worker(
            request_id,
            conv_ids,
            operation_id=operation_id,
        )

    def handle_rename_conversation(conv_id: str, title: str):
        # WOR-76: PC rename. Persist to disk (trim + non-empty + TITLE_MAX_LEN
        # cap + manual_title flag all enforced in the store) then refresh the PC
        # panel in-process â€” exactly like PC delete. The store re-reads from disk
        # on the next add_message, so active_conv's stale in-memory title is
        # harmless (only its id is ever consumed).
        ok = conversations.rename_conversation(conv_id, title)
        if ok:
            updated = conversations.get_conversation(conv_id) or {}
            _notify_mobile_conversation_changed(
                conv_id,
                revision=updated.get("revision", 0),
                operation_id=updated.get("last_operation_id", ""),
                action="rename",
            )
        print(f"âœï¸  Renamed conversation {conv_id}: {'ok' if ok else 'rejected'}")
        popup.refresh_conversations(active_conv["id"] if active_conv else None)

    def handle_folder_mutation(request: dict):
        nonlocal active_conv
        action = str((request or {}).get("action") or "")
        operation_id = f"desktop_folder_{uuid.uuid4().hex}"
        if action == "create":
            result = conversations.create_folder(
                request.get("name", ""), operation_id=operation_id,
            )
        elif action == "rename":
            result = conversations.rename_folder(
                str(request.get("folder_id") or ""),
                request.get("name", ""),
                operation_id=operation_id,
            )
        elif action == "collapse":
            result = conversations.set_folder_collapsed(
                str(request.get("folder_id") or ""),
                bool(request.get("collapsed", False)),
                operation_id=operation_id,
            )
        elif action == "context":
            result = conversations.set_folder_context_policy(
                str(request.get("folder_id") or ""),
                bool(request.get("allow", False)),
                operation_id=operation_id,
            )
        elif action == "reorder":
            result = conversations.reorder_folders(
                list(request.get("folder_ids") or []),
                operation_id=operation_id,
            )
        elif action == "move":
            result = conversations.move_conversation_to_folder(
                str(request.get("conversation_id") or ""),
                request.get("folder_id"),
                operation_id=operation_id,
            )
        elif action == "new_conversation":
            result = conversations.create_conversation_in_folder(
                str(request.get("folder_id") or ""),
                operation_id=operation_id,
            )
            if result.get("success"):
                tts_stop()
                agent.reset()
                active_conv = result.get("conversation") or None
                popup.refresh_conversations(
                    active_conv["id"] if active_conv else None
                )
                popup.set_messages([])
        elif action == "delete":
            folder_id = str(request.get("folder_id") or "")
            snapshot = conversations.conversation_snapshot()
            deleting_ids = {
                str(item.get("id") or "")
                for item in snapshot.get("items") or []
                if str(item.get("folder_id") or "") == folder_id
            }
            if any(_is_conversation_busy(conv_id) for conv_id in deleting_ids):
                popup.show_conversation_delete_busy()
                print(
                    f"Project delete rejected while {folder_id} has active work."
                )
                return
            result = conversations.delete_folder_with_conversations(
                folder_id,
                request.get("expected_count"),
                operation_id=operation_id,
            )
            if result.get("success"):
                deleted_ids = set(result.get("deleted_ids") or [])
                for conv_id in deleted_ids:
                    popup.discard_conversation_draft(conv_id)
                if active_conv and active_conv.get("id") in deleted_ids:
                    tts_stop()
                    agent.reset()
                    active_conv = None
                    popup.refresh_conversations(None)
                    popup.set_messages([])
        else:
            return
        if result.get("success"):
            if action == "new_conversation" and active_conv:
                _notify_mobile_conversation_changed(
                    active_conv["id"],
                    revision=active_conv.get("revision", 0),
                    operation_id=result.get("operation_id", ""),
                )
            else:
                _notify_mobile_conversation_changed(
                    "",
                    revision=result.get("revision", 0),
                    operation_id=result.get("operation_id", ""),
                    action="folders",
                )
        popup.refresh_conversations(active_conv["id"] if active_conv else None)
        if result.get("success") and action == "new_conversation":
            popup.show_chat()

    def handle_bulk_delete_ungrouped(expected_count: int):
        if pending_delete_ids:
            popup.show_conversation_delete_in_progress()
            return
        snapshot = popup.conversation_snapshot()
        deleting_ids = {
            str(item.get("id") or "")
            for item in snapshot.get("items") or []
            if not str(item.get("folder_id") or "")
        }
        busy_ids = {
            conv_id for conv_id in deleting_ids
            if _is_conversation_busy(conv_id)
        }
        if busy_ids:
            popup.show_conversation_delete_busy(
                count=len(busy_ids), bulk=True,
            )
            print("Bulk delete rejected while an ungrouped chat is active.")
            return
        if not deleting_ids:
            popup.show_conversation_delete_none()
            popup.reload_conversations()
            return
        request_id = f"desktop_bulk_delete_{uuid.uuid4().hex}"
        operation_id = f"desktop_bulk_{uuid.uuid4().hex}"
        _begin_conversation_delete_projection(
            request_id, deleting_ids, action="bulk",
        )
        _start_conversation_delete_worker(
            request_id,
            deleting_ids,
            expected_count=expected_count,
            operation_id=operation_id,
        )

    # â”€â”€ Model + settings wiring â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def handle_model_change(model: str, provider: str):
        try:
            agent.set_model(model, provider=provider)
        except Exception as e:
            print(f"âš ï¸  Could not switch model: {e}")
            return
        set_model_selection(model, provider)
        popup.set_current_model(model, provider)
        print(f"ðŸ”„ Model changed to: {model} (provider: {agent.provider})")

    def handle_keys_saved(_keys):
        discover_models_async(
            lambda remote: bridge.remote_models_signal.emit(remote)
        )
        _notify_mobile_state_changed(
            _SERVER_MOBILE_CATALOGUE_URL, "catalogue-changed"
        )

    last_chatgpt_mobile_signature = [None]

    def handle_chatgpt_status_changed(status: dict):
        signature = (
            bool(status.get("connected")),
            str(status.get("error") or ""),
        )
        if signature == last_chatgpt_mobile_signature[0]:
            return
        last_chatgpt_mobile_signature[0] = signature
        _notify_mobile_state_changed(
            _SERVER_CHATGPT_ACCOUNT_URL, "chatgpt-account-changed"
        )

    def handle_remote_models(remote):
        popup.set_remote_models(remote)

    def handle_submit_draft(prompt: str, attachments) -> None:
        from osia.desktop_attachments import (
            DesktopAttachmentError,
            prepare_desktop_turn,
        )

        conv = _ensure_active_conversation(prompt)
        try:
            prepared = prepare_desktop_turn(
                prompt,
                list(attachments or ()),
                provider=agent.provider,
                model=agent.model,
                owner_id=conv["id"],
            )
        except DesktopAttachmentError as error:
            popup.show_attachment_error(error)
            return
        if _send_user_input(
            prepared.provider_content,
            prepared.display_text,
            attachment_turn=True,
            language_text=prompt,
        ):
            popup.accept_draft_submission(prepared.attachment_ids)

    def handle_submit_directive_draft(
        prompt: str, attachments, raw_directives,
    ) -> None:
        from osia.desktop_attachments import (
            DesktopAttachmentError,
            prepare_desktop_turn,
        )
        from osia.slash_commands import (
            SlashDirectiveError,
            SlashTurnDirectivesV1,
            validate_live,
        )

        try:
            directives = SlashTurnDirectivesV1.from_wire(raw_directives)
            directives.require_sendable(
                body=prompt,
                has_attachment=bool(attachments),
            )
            conv = _ensure_active_conversation(prompt)
            validate_live(
                directives,
                current_conversation_id=conv["id"],
                has_attachment=bool(attachments),
            )
        except SlashDirectiveError as error:
            popup._chat.show_notice(error.message(i18n.get_language()))
            return

        if attachments:
            provider = (
                directives.model_override.provider_id
                if directives.model_override is not None else agent.provider
            )
            model = (
                directives.model_override.model_id
                if directives.model_override is not None else agent.model
            )
            try:
                prepared = prepare_desktop_turn(
                    prompt,
                    list(attachments or ()),
                    provider=provider,
                    model=model,
                    owner_id=conv["id"],
                )
            except DesktopAttachmentError as error:
                popup.show_attachment_error(error)
                return
            if _send_user_input(
                prepared.provider_content,
                prepared.display_text,
                attachment_turn=True,
                language_text=prompt,
                turn_directives=directives,
            ):
                popup.accept_directive_submission(prepared.attachment_ids)
            return

        if _send_user_input(
            prompt,
            prompt,
            language_text=prompt,
            turn_directives=directives,
        ):
            popup.accept_directive_submission()

    def handle_slash_mouse_control() -> None:
        nonlocal active_conv
        language_fr = i18n.get_language() == "fr"
        try:
            if active_conv is None:
                active_conv = conversations.create_conversation(special_mode=True)
                popup.refresh_conversations(active_conv["id"])
                popup.bind_unbound_draft(active_conv["id"])
            else:
                active_conv = conversations.set_conversation_special_mode(active_conv["id"])
        except (OSError, ValueError) as error:
            if str(error) == "conversation_busy":
                popup.show_conversation_busy_notice()
            else:
                popup._chat.show_notice(
                    "Impossible de modifier /mousecontrol dans cette discussion."
                    if language_fr else "Could not change /mousecontrol in this conversation."
                )
            return
        _notify_mobile_conversation_changed(
            active_conv["id"], revision=active_conv["revision"],
            operation_id=active_conv["last_operation_id"],
        )
        if active_conv.get("special_mode") is True:
            notice = (
                "Contrôle de la souris désactivé dans cette discussion."
                if language_fr else "Mouse control disabled in this conversation."
            )
        else:
            notice = (
                "Contrôle de la souris réactivé dans cette discussion."
                if language_fr else "Mouse control restored in this conversation."
            )
        popup._chat.show_notice(notice)

    def handle_slash_information(command_id: str) -> None:
        from osia.reasoning import resolve_reasoning_option_id
        from osia.slash_commands import informational_payload

        settings = load_settings() or {}
        provider_id = str(settings.get("selected_model_provider") or agent.provider or "")
        model_id = str(settings.get("selected_model") or agent.model or "")
        inventory = popup._model_inventory
        selected = next((
            item for item in inventory
            if item["provider_id"] == provider_id and item["model_id"] == model_id
        ), {})
        locale = i18n.get_language()
        conv_id = str((active_conv or {}).get("id") or "")
        epoch = selection_epoch[0]

        def load_status() -> None:
            reasoning = "—"
            if command_id == "status.info" and selected:
                try:
                    reasoning = resolve_reasoning_option_id(provider_id, model_id, settings)
                except ValueError:
                    pass
            try:
                payload = informational_payload(
                    command_id, locale=locale, provider_id=provider_id,
                    provider_label=selected.get("provider_display_name") or provider_id or "—",
                    model_label=selected.get("model_display_name") or model_id or "—",
                    reasoning=reasoning,
                    provider_ready=bool(selected.get("available")),
                    routing_mode="automatic" if settings.get("ai_hierarchy_enabled") else "exact",
                    models_available=any(item["available"] for item in inventory),
                    mouse_control_disabled=conversations.conversation_special_mode(conv_id),
                )
                bridge.slash_information_ready.emit(epoch, payload)
            except RuntimeError:
                pass  # The popup can close while the status is loading.

        threading.Thread(target=load_status, name="osia-command-status", daemon=True).start()

    def handle_slash_information_ready(epoch: int, payload: dict) -> None:
        if epoch == selection_epoch[0] and popup.isVisible():
            popup.show_slash_information(payload)

    pending_compactions: set[str] = set()
    meter_cache: dict = {}

    def refresh_context_meter() -> None:
        if not popup.isVisible() or popup._stack.currentIndex() != popup.PAGE_CHAT:
            return
        from osia.conversation_context import (
            conversation_context_usage, estimate_messages_tokens, resolve_context_limit,
        )

        conv = active_conv or {}
        conv_id = str(conv.get("id") or "")
        binding = _active_chat_turn_for_conversation(conv_id).get("binding") or {}
        provider = str(binding.get("provider_id") or agent.provider or "")
        model = str(binding.get("model_id") or agent.model or "")
        usage = conversation_context_usage(conv_id)
        if usage and (usage.get("provider"), usage.get("model")) != (provider, model):
            usage = None
        if provider == "chatgpt_account" and conv_id:
            from osia.chatgpt_account.provider import get_account_provider

            native = get_account_provider().conversation_context_usage(conv_id, model=model)
            usage = native or usage
        if usage is None:
            key = (conv_id, conv.get("revision"), provider, model)
            if meter_cache.get("key") != key:
                meter_cache["key"] = key
                meter_cache["usage"] = {
                    "used": estimate_messages_tokens(conv["messages"]) if conv.get("messages") else 0,
                    "maximum": resolve_context_limit(provider, model),
                    "estimated": True,
                }
            usage = meter_cache["usage"]
        popup._chat._context_meter.set_usage({
            **usage, "compacting": conv_id in pending_compactions or bool(usage.get("compacting")),
        })

    def handle_context_compaction() -> None:
        from osia.conversation_context import ContextBuildError, compact_conversation_context

        conv_id = str((active_conv or {}).get("id") or "")
        if not conv_id:
            popup._chat.show_notice(
                "Ouvre une discussion pour compacter son contexte."
                if i18n.get_language() == "fr" else "Open a conversation to compact its context."
            )
            return
        if conv_id in pending_compactions:
            return
        epoch = selection_epoch[0]
        provider, model = str(agent.provider or ""), str(agent.model or "")
        pending_compactions.add(conv_id)
        refresh_context_meter()

        def run() -> None:
            try:
                result = compact_conversation_context(conv_id, provider, model)
            except ContextBuildError as exc:
                result = {"error": exc.category}
            except Exception as exc:
                print(f"[OSIA] context compaction failed ({type(exc).__name__})")
                result = {"error": "compaction_failed"}
            try:
                bridge.context_compaction_ready.emit(epoch, conv_id, result)
            except RuntimeError:
                pass

        threading.Thread(target=run, name="osia-context-compaction", daemon=True).start()

    def handle_context_compaction_ready(epoch: int, conv_id: str, result: dict) -> None:
        pending_compactions.discard(conv_id)
        if epoch != selection_epoch[0]:
            return
        french = i18n.get_language() == "fr"
        if result.get("error") == "conversation_busy":
            popup.show_conversation_busy_notice()
        elif result.get("error"):
            popup._chat.show_notice(
                "La compaction du contexte a échoué."
                if french else "Context compaction failed."
            )
        elif result.get("changed"):
            popup._chat.show_notice("Contexte compacté." if french else "Context compacted.")
        else:
            popup._chat.show_notice("Le contexte est déjà court." if french else "The context is already short.")
        refresh_context_meter()

    context_meter_timer = QTimer(popup)
    context_meter_timer.setInterval(450)
    context_meter_timer.timeout.connect(refresh_context_meter)
    context_meter_timer.start()

    # â”€â”€ Signal wiring â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    bridge.wake_detected.connect(handle_wake)
    bridge.chat_message.connect(handle_chat_message)
    bridge.chat_message_for_conversation.connect(handle_chat_message_for_conversation)
    bridge.chat_turn_started.connect(handle_chat_turn_started)
    bridge.assistant_stream_chunk.connect(handle_assistant_stream_chunk)
    bridge.assistant_stream_final.connect(handle_assistant_stream_final)
    bridge.assistant_stream_error.connect(handle_assistant_stream_error)
    bridge.island_state.connect(handle_island_state)
    bridge.conversation_busy_changed.connect(handle_conversation_busy_changed)
    bridge.conversation_delete_finished.connect(
        handle_conversation_delete_finished
    )
    bridge.tool_attempt_status.connect(handle_tool_attempt_status)
    bridge.execution_trace.connect(handle_execution_trace)
    bridge.remote_models_signal.connect(handle_remote_models)
    bridge.conversations_changed.connect(popup.refresh_conversations)
    bridge.new_chat.connect(handle_voice_new_chat)
    # WOR-22: Ctrl+Alt+O toggles the popup, keeping the GUI reachable even when
    # the floating capsule is hidden.
    bridge.toggle_popup.connect(popup.toggle)
    stop_popup_hotkey = _start_popup_hotkey_listener(bridge.toggle_popup.emit)

    popup.send_text.connect(handle_send_text)
    popup.submit_draft.connect(handle_submit_draft)
    popup.submit_directive_draft.connect(handle_submit_directive_draft)
    popup.informational_requested.connect(handle_slash_information)
    bridge.slash_information_ready.connect(handle_slash_information_ready)
    popup.mouse_control_requested.connect(handle_slash_mouse_control)
    popup.context_compaction_requested.connect(handle_context_compaction)
    bridge.context_compaction_ready.connect(handle_context_compaction_ready)
    popup.new_conversation.connect(handle_new_conversation)
    popup.open_conversation.connect(handle_open_conversation)
    popup.rename_conversation.connect(handle_rename_conversation)
    popup.delete_conversation.connect(handle_delete_conversation)
    popup.folder_mutation_requested.connect(handle_folder_mutation)
    popup.bulk_delete_ungrouped_requested.connect(
        handle_bulk_delete_ungrouped
    )
    popup.model_changed.connect(handle_model_change)
    popup.api_keys_saved.connect(handle_keys_saved)
    popup.chatgpt_status_changed.connect(handle_chatgpt_status_changed)
    # WOR-22: the "Show the floating capsule" toggle hides/shows the capsule.
    popup.capsule_visibility_changed.connect(island.set_capsule_visible)

    # Initial population â€” no active conversation yet; the panel shows the
    # stored list (or its "No conversations" placeholder when empty).
    popup.refresh_conversations(None)
    discover_models_async(
        lambda remote: bridge.remote_models_signal.emit(remote)
    )

    # â”€â”€ Scheduled tasks + heartbeat (Bloc 5) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Cron tasks run on a DEDICATED agent so a scheduled run never disturbs
    # the user's active conversation/history; the reply is spoken just like a
    # voice command. Both background threads serialise their speech with the
    # interactive handlers through handler_lock so audio never overlaps a turn.
    cron_agent = OSIAAgent()
    try:
        if initial_model:
            cron_agent.set_model(initial_model)
    except Exception as e:
        print(f"[CRON] could not preset model {initial_model!r}: {e}")

    def run_cron_task(
        task_text: str,
        conversation_id: str | None = None,
        model: dict | None = None,
        scheduled_execution: dict | None = None,
    ) -> dict:
        if not handler_lock.acquire(timeout=120):
            print(f"[CRON] busy â€” scheduled task skipped: {task_text[:60]}")
            return {
                "status": "failed",
                "message": "OSIA was busy; scheduled task skipped",
            }
        conversation_stale = False
        terminal_turn_id = str(
            (scheduled_execution or {}).get("occurrence_id")
            or f"cron-{uuid.uuid4().hex}"
        )
        try:
            tts_stop()
            model_meta = model if isinstance(model, dict) else {}
            sel = (model_meta.get("id") or load_settings().get("selected_model") or "")
            provider = model_meta.get("provider") or None
            if sel:
                try:
                    cron_agent.set_model(sel, provider=provider)
                except Exception as e:
                    print(f"[CRON] set_model({sel!r}) failed: {e}")
            cron_agent._disable_hierarchy_for_turn = True
            # Linked conversation (optional): load its history so context
            # carries across scheduled runs, and append this run's exchange so
            # the next run sees it. With none linked, each run is a fresh,
            # one-shot turn exactly as before.
            conv = (conversations.get_conversation(conversation_id)
                    if conversation_id else None)
            if conversation_id and conv is None:
                conversation_stale = True
                print(
                    f"[CRON] linked conversation {conversation_id} not found; "
                    "running task without conversation persistence"
                )
            island_lifecycle.begin("cron", terminal_turn_id, "thinking")
            if conv is not None:
                history = conv.get("messages") or []
                print(f"[CRON] conversation {conversation_id}: loaded "
                      f"{len(history)} messages for context")
                if scheduled_execution is not None:
                    reply = cron_agent.run_scheduled(
                        task_text,
                        scheduled_execution,
                        terminal_turn=True,
                        turn_id=terminal_turn_id,
                        history=history,
                        current_conversation_id=conversation_id,
                    )
                else:
                    reply = _run_turn_to_terminality(
                        cron_agent,
                        task_text,
                        turn_id=terminal_turn_id,
                        history=history,
                        current_conversation_id=conversation_id,
                    )
                _persist_exchange(
                    conversation_id,
                    task_text,
                    clean_response(reply or ""),
                    getattr(cron_agent, "turn_context", None),
                )
            else:
                cron_agent.reset()  # each scheduled run is a fresh, one-shot turn
                if scheduled_execution is not None:
                    reply = cron_agent.run_scheduled(
                        task_text,
                        scheduled_execution,
                        terminal_turn=True,
                        turn_id=terminal_turn_id,
                    )
                else:
                    reply = _run_turn_to_terminality(
                        cron_agent,
                        task_text,
                        turn_id=terminal_turn_id,
                    )
            display = clean_response(reply or "")
            island_lifecycle.update("cron", terminal_turn_id, "idle")
            if display:
                if conv is not None and conversation_id:
                    bridge.chat_message_for_conversation.emit(
                        conversation_id, "assistant", f"â° {display}"
                    )
                    bridge.conversations_changed.emit(
                        active_conv["id"] if active_conv else ""
                    )
                island_lifecycle.update("cron", terminal_turn_id, "speaking")
                speak(
                    display,
                    language=getattr(
                        getattr(cron_agent, "turn_context", None),
                        "language",
                        None,
                    ),
                )
            terminal = _terminality_result(cron_agent, terminal_turn_id, reply)
            terminal_state = str((terminal or {}).get("terminal_state") or "")
            if conversation_stale:
                return {
                    "status": terminal_state or "blocked_verified",
                    "conversation_stale": True,
                    "message": "linked conversation not found; ran unlinked",
                }
            return {"status": terminal_state or "blocked_verified"}
        except Exception as e:
            print(f"[CRON] task error: {e}")
            return {"status": "failed", "message": str(e)}
        finally:
            cron_agent._disable_hierarchy_for_turn = False
            island_lifecycle.finish("cron", terminal_turn_id)
            handler_lock.release()

    def heartbeat_alert(message: str) -> None:
        # Wait briefly for any active turn to finish so the alert isn't spoken
        # over it; if still busy, log rather than drop it silently.
        if not handler_lock.acquire(timeout=20):
            print(
                f"[HEARTBEAT] busy â€” alert not spoken "
                f"({len(message)} chars)"
            )
            return
        try:
            bridge.chat_message.emit("assistant", f"ðŸ’“ {message}")
            speak(message)
        finally:
            handler_lock.release()

    def notify_cron_task(
        *,
        task_id: str | None,
        title: str | None,
        status: str,
        conversation_id: str | None,
    ) -> None:
        from osia.notifications import notify, safe_task_title, scheduled_notification_text

        safe_title = safe_task_title(title)
        body = f"{scheduled_notification_text(status)}: {safe_title}"
        notify("OSIA", body)
        _notify_mobile_cron_notification(
            task_id=task_id,
            title=safe_title,
            status=status,
            conversation_id=conversation_id,
        )

    cron_manager = CronManager(run_cron_task, notify_task=notify_cron_task)
    unsubscribe_cron_wake = scheduled_tasks.add_change_callback(cron_manager.wake)
    def _tool_attempt_lifecycle_tick(expected_interval: float) -> None:
        try:
            from osia.tool_attempts import get_tool_attempt_coordinator

            get_tool_attempt_coordinator().observe_lifecycle_tick(
                expected_interval=expected_interval,
            )
        except Exception:
            pass

    heartbeat_monitor = HeartbeatMonitor(
        heartbeat_alert,
        lifecycle_tick=_tool_attempt_lifecycle_tick,
    )
    cron_manager.start()
    heartbeat_monitor.start()

    def handle_chat_execution_cancel(
        binding: dict, *, discard_stream: bool = True,
    ) -> None:
        key = _chat_turn_key(binding)
        with active_chat_turns_lock:
            direct = active_chat_turns.get(key)
            if direct is not None and direct.get("cancel_pending"):
                return
            if direct is not None:
                direct["cancel_pending"] = True
        if direct is None:
            if discard_stream:
                popup.reject_chat_execution_cancellation(binding)
            return
        turn_agent = direct["agent"]
        exact_binding = dict(direct["binding"])
        turn_id = exact_binding["turn_id"]

        def _cancel_direct() -> None:
            accepted = False
            try:
                accepted = bool(turn_agent.request_turn_cancellation_intent(turn_id))
            except Exception as exc:
                print(f"[OSIA] Stop intent failed: {type(exc).__name__}", flush=True)
                # A failure while projecting Stop can follow a successful
                # durable write. Check the exact persisted result in that case.
                from osia.turn_terminality import load_persisted_terminal_result

                terminal = load_persisted_terminal_result(turn_id) or {}
                persisted_binding = terminal.get("execution_binding") or {}
                accepted = (
                    terminal.get("terminal_state") == "user_cancelled"
                    and bool(persisted_binding)
                    and all(
                        exact_binding.get(field) == value
                        for field, value in persisted_binding.items()
                    )
                )
            try:
                bridge.chat_cancel_finished.emit(
                    exact_binding, turn_agent, accepted, discard_stream,
                )
            except RuntimeError:
                pass  # The GUI was disposed while cancellation was pending.
            if accepted:
                try:
                    turn_agent.request_turn_cancellation(turn_id)
                except Exception as exc:
                    print(f"[OSIA] Stop propagation failed: {type(exc).__name__}", flush=True)

        try:
            threading.Thread(
                target=_cancel_direct,
                name="osia-chat-turn-cancel",
                daemon=True,
            ).start()
        except RuntimeError:
            handle_chat_execution_cancel_finished(exact_binding, turn_agent, False, discard_stream)

    def handle_chat_execution_cancel_finished(
        binding: dict, turn_agent, accepted: bool, discard_stream: bool,
    ) -> None:
        if not accepted:
            with active_chat_turns_lock:
                direct = active_chat_turns.get(_chat_turn_key(binding))
                if direct is not None and direct.get("agent") is turn_agent:
                    direct.pop("cancel_pending", None)
            if discard_stream:
                popup.reject_chat_execution_cancellation(binding)
            return
        _release_chat_turn(binding, turn_agent)
        popup.confirm_chat_execution_cancellation(binding, discard_stream=discard_stream)
        _set_conversation_busy(binding["conversation_id"], False, owner=binding)

    popup.chat_execution_cancel_requested.connect(handle_chat_execution_cancel)
    bridge.chat_cancel_finished.connect(
        handle_chat_execution_cancel_finished, Qt.ConnectionType.QueuedConnection,
    )

    trigger_label = "Press Ctrl+Alt+P to talk to OSIA"

    print("=" * 60)
    print("  OSIA â€” Wake Mode (Dynamic Island)")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print(f"  {trigger_label}")
    print("  Ctrl+Alt+O: show/hide the OSIA window")
    print("=" * 60)

    from osia.chatgpt_account.runtime_update import start_runtime_update_worker

    codex_update_stop = start_runtime_update_worker()

    def on_quit():
        codex_update_stop.set()
        voice_ui_lifecycle.close()
        island_lifecycle.close()
        owned_stops = [
            cron_manager.stop,
            unsubscribe_cron_wake,
            heartbeat_monitor.stop,
        ]
        if stop_manual_hotkey:
            owned_stops.append(stop_manual_hotkey)
        if stop_popup_hotkey:
            owned_stops.append(stop_popup_hotkey)
        worker_stopped = _shutdown_voice_runtime(
            close_acceptance=manual_activation_open.clear,
            cancel_pending_activation=lambda: wake_gate.cancel(0),
            signal_worker_cancel=voice_shutdown_requested.set,
            invalidate_tts=tts_stop,
            get_active_worker=_get_active_voice_worker,
            stop_before_tts_shutdown=tuple(owned_stops),
            shutdown_tts=tts_shutdown,
            release_voice_gate=wake_gate.close,
            join_timeout=1.0,
        )
        if not worker_stopped:
            print(
                "[shutdown] voice worker exceeded bounded join; "
                "its obsolete TTS session remains invalidated",
                flush=True,
            )
        try:
            from osia.chatgpt_account.provider import get_account_provider
            get_account_provider().shutdown()
        except Exception:
            pass
        try:
            from osia.mcp_client import get_manager
            get_manager().shutdown()
        except Exception:
            pass
        try:
            from osia.tools.browser_controller import close_browser
            close_browser()
        except Exception:
            pass

    app.aboutToQuit.connect(on_quit)

    # Reattach exact local direct-Chat receipts.
    QTimer.singleShot(0, _restore_incomplete_desktop_chat_turns)

    perf_mark("wake_mode: entering app.exec() (GUI now interactive)")
    sys.exit(app.exec())


def main():
    wake_entry = "--text" not in sys.argv and "--voice" not in sys.argv
    try:
        from osia.relaunch import acquire_desktop_instance_mutex
        if wake_entry:
            desktop_mutex = acquire_desktop_instance_mutex()
            if sys.platform == "win32" and desktop_mutex is False:
                return 0
    except Exception as exc:
        if sys.platform == "win32" and wake_entry:
            print(f"[OSIA] desktop singleton unavailable: {exc}")
            return 1

    try:
        from osia.relaunch import clear_stale_restart_state
        clear_stale_restart_state()
    except Exception:
        pass

    try:
        from osia.image_inputs import (
            cleanup_media_store,
            cleanup_owned_temp_images,
        )

        cleanup_media_store()
        cleanup_owned_temp_images()
    except Exception:
        pass

    # First-launch config bootstrap: create any missing config files
    # (settings.json, mobile pairing token) BEFORE anything starts the mobile
    # WebSocket server, so a fresh install works out of the box on any OS and the
    # server authenticates against a persisted token. On Windows/Mac the GUI is
    # the de-facto sole/primary process, so it creates the token here (on Windows
    # this runs before _ensure_mobile_server_windows launches the server). On
    # Linux a separate systemd server process (run_server.py) is the single token
    # creator, so pass create_token=False to avoid two processes racing to
    # persist divergent tokens. settings.json is always created (GUI is its only
    # creator). Idempotent + best-effort â€” it must never block startup.
    try:
        from osia.config import ensure_config_files
        ensure_config_files(create_token=not sys.platform.startswith("linux"))
    except Exception as e:
        print(f"[CONFIG] ensure_config_files skipped: {e}")

    try:
        from osia.tool_attempts import recover_orphaned_tool_attempts

        recovered_attempts = recover_orphaned_tool_attempts()
        if recovered_attempts:
            print(
                f"[OSIA] reconciled {len(recovered_attempts)} orphaned "
                "tool attempt(s) without replay"
            )
    except Exception as e:
        print(
            "[OSIA] tool-attempt startup recovery skipped: "
            f"{type(e).__name__}"
        )

    # One-time tool health check: logs which tool categories (bash, file
    # read, browser) are usable so missing dependencies show up at startup
    # instead of failing mid-conversation. It imports playwright and probes
    # PATH, so it runs on a daemon thread to keep it off the GUI startup path
    # (WOR perf fix) â€” it must never block startup.
    def _health_check():
        try:
            verify_tools()
        except Exception as e:
            print(f"[OSIA] tool health check failed: {e}")
    threading.Thread(target=_health_check, daemon=True).start()

    if "--text" in sys.argv:
        text_mode()
    elif "--voice" in sys.argv:
        voice_mode()
    elif "--wake" in sys.argv:
        wake_mode()
    else:
        # Default: wake mode with GUI
        wake_mode()


if __name__ == "__main__":
    main()
