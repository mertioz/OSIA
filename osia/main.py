#!/usr/bin/env python3
import os
import sys
# The Qt GUI must run under the X11 (xcb) platform plugin on Linux. On Windows
# Qt uses its native "windows" plugin automatically, so xcb must NOT be forced
# (it would fail to start). Guard it to Linux only — WOR-11 (Windows port).
if sys.platform.startswith("linux"):
    os.environ["QT_QPA_PLATFORM"] = "xcb"
# On Windows, the console's default encoding (cp1252) raises UnicodeEncodeError
# when printing emoji / non-ASCII (e.g. the "▶" in "[OSIA Agent] ▶ tool:").
# Reconfigure stdout/stderr to UTF-8 (replace on error) before any code prints
# non-ASCII — WOR-11. Guarded for the detached-launch case (autostart under
# pythonw), where stdout/stderr may be None or lack .reconfigure — this must
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
# ONCE, here at startup — BEFORE Qt's QApplication is ever constructed and BEFORE
# the `uiautomation` library can be imported (it sets DPI awareness itself at
# import time). DPI awareness can only be set once per process, so fixing it
# FIRST means Qt (which would otherwise enable Per-Monitor-v2 on its own) and
# uiautomation both inherit THIS mode instead of racing to set divergent ones.
# The payoff: UIA BoundingRectangle, Pillow ImageGrab (vision capture) and
# pyautogui clicks then all share ONE physical-pixel coordinate space, so a rect
# center resolved by UIA lands exactly where the click is sent. We request
# Per-Monitor-Aware-v2, which is already Qt6's default — so the GUI renders
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
OSIA main entry point — supports text mode, voice mode, wake-word mode.

Run:
  python osia/main.py              # Wake word mode with wake screen (default)
  python osia/main.py --text       # Text mode
  python osia/main.py --voice      # Voice mode: speak command, get spoken response
  python osia/main.py --wake       # Wake word mode with wake screen
"""

# Silence the noisy startup chatter from torch / transformers / kokoro.
# Must run before any module that transitively imports them — otherwise the
# import-time warnings have already been emitted.
import warnings
warnings.filterwarnings("ignore", module=r"torch.*")
warnings.filterwarnings("ignore", module=r"transformers.*")
warnings.filterwarnings("ignore", module=r"kokoro.*")

import sys
import os
import threading
import time

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
_SERVER_MOBILE_CATALOGUE_URL = (
    "http://127.0.0.1:8765/internal/mobile_catalogue_changed"
)
_SERVER_CHATGPT_ACCOUNT_URL = (
    "http://127.0.0.1:8765/internal/chatgpt_account_changed"
)
LIVEAPI_AVAILABLE = False
# Desktop→server MCP state mirror (Phase 1, direction A). The desktop pings this
# after a local MCP connect/disconnect so the mobile server reconciles its OWN
# manager and repaints every phone. Same loopback host as new_chat; carries only
# {id, action} (no secret); localhost-only on the server side.
_SERVER_MCP_SYNC_URL = "http://127.0.0.1:8765/internal/mcp_sync"
# WOR-96 PART 1 — desktop entry point for an OAuth connect that completes via the
# HTTPS tunnel callback (app.osia.space → the SERVER process). The desktop kicks the
# server (which runs the OAuth provider so the tunnel redirect lands in the same
# process as the PKCE state) and opens the returned authorize URL in the PC
# browser. Separate from the existing loopback Connect button, which is unchanged.
_SERVER_MCP_OAUTH_TUNNEL_URL = "http://127.0.0.1:8765/internal/mcp_oauth_tunnel_connect"
# WOR-97 — desktop entry point to abort an in-flight OAuth login running in the
# SERVER process (tunnel/PART-1 flow). The desktop POSTs {id} to this loopback
# route, which calls MCPManager.cancel_pending server-side and rebroadcasts.
_SERVER_MCP_OAUTH_CANCEL_URL = "http://127.0.0.1:8765/internal/mcp_oauth_cancel"
# WOR-98 — desktop entry point to "switch account" on a CONNECTED OAuth connector.
# The desktop POSTs {id} to this loopback route, which tears the session down,
# REVOKES the grant (RFC 7009) AND forgets the DCR client (delete_mcp_oauth) so the
# next Connect re-runs DCR + a fresh login, then rebroadcasts the reset.
_SERVER_MCP_OAUTH_SWITCH_URL = "http://127.0.0.1:8765/internal/mcp_oauth_switch"
_SERVER_CRON_NOTIFICATION_URL = "http://127.0.0.1:8765/internal/cron_notification"
_SERVER_AUTONOMOUS_PC_STATE_URL = (
    "http://127.0.0.1:8765/internal/autonomous_authorization_state"
)
_SERVER_AUTONOMOUS_PC_ACTION_URL = (
    "http://127.0.0.1:8765/internal/autonomous_authorization_action"
)


def _load_server_autonomous_pc_state() -> dict | None:
    """Fetch the server-owned mobile run for local PC review only."""

    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
            _SERVER_AUTONOMOUS_PC_STATE_URL, timeout=0.75,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    state = payload.get("state") if isinstance(payload, dict) else None
    return state if payload.get("ok") is True and isinstance(state, dict) else None


def _send_server_autonomous_pc_action(action: str) -> dict | None:
    """Send one explicit PC-panel action to the canonical server run."""

    import json
    import urllib.request

    payload = json.dumps({"action": str(action or "")}).encode("utf-8")
    try:
        request = urllib.request.Request(
            _SERVER_AUTONOMOUS_PC_ACTION_URL,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


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


def _notify_mobile_mcp_sync(server_id: str, action: str) -> None:
    """Best-effort desktop→server MCP state mirror (Phase 1, direction A).

    After the DESKTOP manager connects/disconnects an MCP connector, tell the
    mobile WS server so it reconciles its OWN manager (headless, fresh-token
    gated — never a browser) and broadcasts the resulting status to every
    connected phone. Carries only ``{id, action}`` — no secret. Silent no-op when
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
    """WOR-96 PART 1 — start an OAuth connect that completes via the HTTPS tunnel
    callback (app.osia.space → the SERVER process), and open the authorize URL in the
    PC browser.

    POSTs ``{id}`` to the loopback trigger ``/internal/mcp_oauth_tunnel_connect``.
    The SERVER runs the OAuth provider IN ITS OWN process — where the tunnel
    redirect lands and the SDK's in-memory PKCE verifier/state live — and returns
    the authorize URL, which we open HERE in the PC browser (the desktop process
    is the one allowed to open a browser; the server neutralises
    ``webbrowser.open``). The token exchange completes server-side when the user
    finishes login and the provider redirects to ``/oauth/callback``.

    Returns True iff the browser was opened. Best-effort; never raises. This is a
    SEPARATE path from the existing loopback Connect button (unchanged): it runs no
    provider in the desktop process and gives the desktop no HTTP listener — it
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
    """WOR-97 — desktop→server bridge to abort an in-flight OAuth login that runs
    in the SERVER process (a tunnel/PART-1 flow the desktop manager can't cancel
    locally). POSTs ``{id}`` to the loopback ``/internal/mcp_oauth_cancel``; the
    server calls ``MCPManager.cancel_pending`` and broadcasts the reset so every
    pill repaints to disconnected.

    Carries only an id (no secret). Best-effort, never raises; a silent no-op when
    the server isn't running. Call OFF the Qt main thread (the 1.5s timeout never
    blocks the UI). Returns True iff the server acked ok. The desktop only uses
    this for connectors the LOOPBACK manager doesn't own — a PC-initiated login is
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
    """WOR-98 — desktop→server bridge for "Switch account" on a CONNECTED OAuth
    connector. POSTs ``{id}`` to the loopback ``/internal/mcp_oauth_switch``; the
    server tears the session down, REVOKES the grant (RFC 7009) AND forgets the DCR
    client blob (``delete_mcp_oauth``) so the next Connect re-runs DCR + a fresh
    login (a different account), then broadcasts the reset so every pill repaints to
    disconnected.

    Carries only an id (no secret). Best-effort, never raises; a silent no-op when
    the server isn't running. Call OFF the Qt main thread (the timeout never blocks
    the UI). Returns True iff the server acked ok. Used for BOTH loopback and tunnel
    OAuth connectors — the revoke+forget logic lives server-side and
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
    ("OSIA Mobile Server", registered by ``install.bat``) — the Windows
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
    # once the file exists) and best-effort — it must never block server startup.
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
    conv_id, agent, prev_history_len, *, user_display_text: str | None = None,
):
    """Persist messages added to ``agent.messages`` during the most recent
    ``agent.run()`` call. ``prev_history_len`` is the length of the
    non-system message list (``agent.messages[1:]``) before that call.

    Errors are logged but never raised — persistence must never crash
    the interactive loop.
    """
    if not conv_id:
        return
    try:
        new_msgs = agent.messages[1:][prev_history_len:]
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
            # user's own bubble — in the desktop popup AND on mobile, which
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
            if pending_user_display is not None and clean.get("role") == "user":
                clean = dict(clean)
                clean["content"] = pending_user_display
                pending_user_display = None
            context = getattr(agent, "turn_context", None)
            metadata = None
            if context is not None:
                metadata = {
                    "_language": context.language,
                    "_language_mode": context.mode,
                    "_allowed_languages": list(context.allowed_languages),
                }
            if artifact_refs and index == final_assistant_index:
                metadata = dict(metadata or {})
                metadata["_artifact_refs"] = artifact_refs
            persistence_kwargs = {"metadata": metadata} if metadata else {}
            conversations.add_message(
                conv_id, clean.get("role"), clean.get("content", ""),
                **persistence_kwargs,
            )
    except Exception as e:
        print(f"[OSIA] failed to persist conversation: {e}")


def _finalize_controlled_refusal(
    error,
    agent,
    conv_id,
    prev_history_len,
    emit_final,
    *,
    user_display_text: str | None = None,
) -> str | None:
    """Persist and emit one explicitly classified refusal as assistant text."""

    if not (
        bool(getattr(error, "controlled_refusal", False))
        and bool(getattr(error, "native_action", False))
    ):
        return None
    text = str(error)
    agent.messages.append({"role": "assistant", "content": text})
    _persist_agent_delta(
        conv_id,
        agent,
        prev_history_len,
        user_display_text=user_display_text,
    )
    from osia.agent import clean_response

    display = clean_response(text)
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
            metadata = {
                "_language": turn_context.language,
                "_language_mode": turn_context.mode,
                "_allowed_languages": list(turn_context.allowed_languages),
            }
        conversations.add_message(
            conv_id, "user", user_text, metadata=metadata,
        )
        conversations.add_message(
            conv_id, "assistant", assistant_text or "", metadata=metadata,
        )
    except Exception as e:
        print(f"[OSIA] failed to persist conversation: {e}")


# ── Ctrl+Space hotkey fallback ──────────────────────────────────────────────

def _start_ctrl_space_listener(callback):
    """
    Start a global keyboard listener that triggers `callback` on Ctrl+Space.
    This is the intentional release activation path while acoustic wake is off.

    Returns a stop function.
    """
    from pynput import keyboard

    ctrl_pressed = False

    def on_press(key):
        nonlocal ctrl_pressed
        try:
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                ctrl_pressed = True
            elif key == keyboard.Key.space and ctrl_pressed:
                print("⌨️  Ctrl+Space pressed — triggering OSIA")
                callback()
        except Exception as exc:
            print(
                "[wake] Ctrl+Space callback failed: "
                f"{type(exc).__name__}",
                flush=True,
            )

    def on_release(key):
        nonlocal ctrl_pressed
        try:
            if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                ctrl_pressed = False
        except Exception:
            pass

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    return listener.stop


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


# ── Modes ───────────────────────────────────────────────────────────────────

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
    # No conversation is auto-created — one is created lazily when the
    # first message is sent, so the stored list never fills with empties.
    active_conv = None
    print("=" * 60)
    print("  OSIA — Local AI Voice Assistant (text mode)")
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
            print("[History cleared — next message starts a new conversation]")
            continue

        # First message of this conversation — create it now, never earlier.
        if active_conv is None:
            active_conv = conversations.create_conversation()
            print(f"[New conversation: {active_conv['id']}]")

        prev_history_len = len(agent.messages) - 1  # skip system prompt
        print("\nOSIA is thinking...", flush=True)
        try:
            response = agent.run(
                user_input,
                current_conversation_id=active_conv["id"],
            )
            print(f"\nOSIA: {response}")
            _persist_agent_delta(active_conv["id"], agent, prev_history_len)
        except Exception as e:
            print(f"\n[ERROR] {e}")


def voice_mode():
    """Single-shot voice mode: listen → agent → speak."""
    from osia import i18n
    from osia.turn_context import resolve_turn_context
    from osia.voice.stt import listen
    from osia.voice.tts import speak
    from osia.agent import clean_response

    # No conversation is auto-created — one is created lazily when the
    # first spoken command arrives.
    active_conv = None
    print("=" * 60)
    print("  OSIA — Voice Mode")
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
            print("❌ Nothing heard.")
            speak("I didn't catch that. Could you repeat?")
            continue

        print(f"📝 You said: {user_text}")

        # First message — create the conversation now, never earlier.
        if active_conv is None:
            active_conv = conversations.create_conversation()
            print(f"[New conversation: {active_conv['id']}]")

        print("🤔 Thinking...")
        turn_agent = OSIAAgent()
        settings = load_settings()
        selected_model = settings.get("selected_model")
        if selected_model:
            turn_agent.set_model(
                selected_model,
                provider=settings.get("selected_model_provider") or None,
            )
        turn_context = resolve_turn_context(
            user_text,
            saved_language=i18n.get_language(),
            reference=active_conv["id"],
        )
        response = turn_agent.run(
            user_text,
            current_conversation_id=active_conv["id"],
            turn_context=turn_context,
        )
        response = clean_response(response)
        print(f"💬 OSIA: {response}")

        _persist_exchange(
            active_conv["id"], user_text, response, turn_context,
        )
        print(f"[TTS] main.voice_mode: calling speak() ({len(response)} chars)", flush=True)
        speak(response, language=turn_context.language)
        print("[TTS] main.voice_mode: speak() returned", flush=True)


def wake_mode():
    """Desktop voice mode with intentional Ctrl+Space manual activation."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QObject, pyqtSignal, Qt, QTimer
    from PyQt6.QtGui import QGuiApplication, QIcon

    from osia.perf import perf_mark
    perf_mark("wake_mode: enter")

    from osia.utils.assets import ICON_PATH, ensure_desktop_entry
    from osia.gui.dynamic_island import DynamicIsland
    from osia.gui.main_popup import MainPopup
    from osia.voice.wake import WakeAcceptanceGate
    # NOTE: osia.voice.stt (faster_whisper, ~4.3s to import on Windows) is NOT
    # imported here. listen_for_command is only used on the wake daemon thread —
    # it is imported lazily inside handle_wake()'s process() worker so the heavy
    # import never blocks GUI startup (WOR perf fix).
    from osia.voice.commands import is_new_chat_command
    from osia.voice.tts import (
        shutdown as tts_shutdown,
        playback_active as tts_playback_active,
        speak,
        speak_greeting_cached,
        speak_streaming,
        start_streaming_session,
        stop as tts_stop,
        wait_for_playback_complete,
    )
    from osia.agent import clean_response
    from osia import i18n
    from osia.turn_context import (
        fixed_turn_context,
        localized_text,
        normalize_locale,
        resolve_turn_context,
    )
    from osia import scheduled_tasks
    from osia.config import (
        ACOUSTIC_WAKE_AVAILABLE,
        ACOUSTIC_WAKE_UNAVAILABLE_REASON,
        load_settings,
        set_setting,
    )
    from osia.models import discover_models_async
    from osia.mobile_model_catalogue import (
        apply_autonomous_agent_model,
        resolve_current_autonomous_selection,
    )
    from osia.cron import CronManager
    from osia.heartbeat import HeartbeatMonitor
    # WOR-45 Phase 4 — wire the autonomous engine (consumed as black boxes).
    from osia.autonomous.orchestrator import Orchestrator
    from osia.autonomous import (
        authorization as autonomous_authorization,
        diagnostics as autonomous_diagnostics,
        planner,
    )
    from osia.autonomous.state import (
        clear_task_state,
        count_progress,
        new_failed_task_state,
        resume_state_is_valid,
        save_task_state,
    )
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
        assistant_stream_chunk = pyqtSignal(str, str)
        assistant_stream_final = pyqtSignal(str, str)
        assistant_stream_error = pyqtSignal(str, str)
        # Drive the island's visual state from a worker thread.
        island_state = pyqtSignal(str)
        # Per-conversation desktop busy state for the popup input controls.
        conversation_busy_changed = pyqtSignal(str, bool)
        # Refresh the conversations panel (new active id) from a worker thread.
        conversations_changed = pyqtSignal(str)
        # Reset chat (agent + GUI) from a worker thread — "nouveau chat" voice
        # command (WOR-10). Runs on the GUI thread so Qt calls are safe.
        new_chat = pyqtSignal()
        # Toggle the popup from the global Ctrl+Alt+O hotkey thread (WOR-22).
        # Marshalled onto the GUI thread so popup.toggle() is Qt-safe.
        toggle_popup = pyqtSignal()
        # Autonomous-agent progress (WOR-45 Phase 4). The Orchestrator's
        # progress_cb runs on its daemon thread; it only ever emits this signal
        # (never touches a widget). Carries the event dict + a state snapshot.
        autonomous_progress = pyqtSignal(dict)
        autonomous_resume_ready = pyqtSignal(dict)

    bridge = Bridge()

    # Identify the app to the windowing system *before* any window is
    # created, so the taskbar/dock shows "OSIA" + the logo instead of
    # "python"/"osia_desktop" and a generic icon.
    #   • applicationName / displayName  → the window title + dock label
    #   • desktopFileName ("osia")       → Wayland/XCB map the window to
    #     ~/.local/share/applications/osia.desktop for its taskbar icon
    #   • setWindowIcon (absolute path)  → the icon every window inherits,
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
    # DPR-tagged pixmaps assume — it equals the Qt6 default, so it is a no-op on
    # every platform today (integer-scale Linux/Mac included) and changes no
    # behaviour. Must run before the QApplication is constructed. (WOR-11.)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("OSIA")
    app.setApplicationDisplayName("OSIA")
    app.setWindowIcon(QIcon(ICON_PATH))

    # WOR-11 (Windows): bring up the mobile server alongside the GUI. On Linux a
    # systemd unit owns it; on Windows main.py does, so it also starts on the
    # login-autostart path (which only runs main.py). Idempotent + off-thread so
    # it never blocks GUI startup. No-op on non-Windows platforms.
    threading.Thread(target=_ensure_mobile_server_windows, daemon=True).start()

    island = DynamicIsland()
    popup = MainPopup()
    perf_mark("wake_mode: GUI constructed (island + popup)")

    # Release-only legal gate. The bundled documents are currently review
    # drafts, so this is a no-op. Developer/test runs stay unblocked even after
    # approval unless they explicitly opt into OSIA_RELEASE_BUILD=1.
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

    settings = load_settings()
    initial_model = settings.get("selected_model")
    initial_provider = settings.get("selected_model_provider") or None
    if initial_model:
        try:
            agent.set_model(initial_model, provider=initial_provider)
        except Exception as e:
            print(f"[DEBUG] set_model({initial_model!r}) failed: {e}")
    popup.set_current_model(agent.model, agent.provider)

    # WOR-44 Phase 5 (5-conf-b): register the DESKTOP confirmation provider so a
    # write/destructive MCP tool prompts the user (modal) before it runs. The
    # provider is invoked on the agent's worker thread and bridges to THIS Qt
    # main thread. Registered ONLY on the desktop path — the mobile server keeps
    # NO provider (hard-deny) until 5-conf-c. Done BEFORE app.exec(), so it is
    # set before any user-triggered agent turn can run. Best-effort: a failure
    # must never block GUI startup (the gate then hard-denies writes, which is
    # the safe default). The controller is kept alive for wake_mode's lifetime.
    mcp_confirm_controller = None
    try:
        from osia.gui.mcp_confirm import MCPConfirmController
        from osia.mcp_client import registry as _mcp_registry
        mcp_confirm_controller = MCPConfirmController(popup)
        _mcp_registry.set_confirmation_provider(mcp_confirm_controller.confirm)
    except Exception as e:
        print(f"[OSIA] MCP confirm provider not registered: {e}")

    # WOR-44: auto-reconnect every ``enabled`` MCP connector at launch, so a
    # connector the user previously connected (its token / OAuth blob persisted)
    # is live again WITHOUT reopening Settings and clicking Connect. Reuses the
    # SAME MCPManager.connect() path as the Connect button — idempotent, and the
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

    # WOR-44 Phase 2 (direction B, mobile→PC): an ALWAYS-ON daemon that reconciles
    # THIS desktop process's MCPManager to the shared mcp_servers[].enabled flag.
    # When the user connects/disconnects an MCP connector FROM THE MOBILE app, the
    # server drives its own manager AND now persists enabled (Phase-2 server-side
    # write); this loop (~3s) sees the delta and connects/disconnects the desktop
    # manager to match — so the desktop mirrors mobile-side changes even when the
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
    # within its poll interval — no GUI change needed.
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
    busy_lock = threading.Lock()

    def _any_conversation_busy() -> bool:
        with busy_lock:
            return bool(busy_conversations)

    wake_gate = WakeAcceptanceGate(
        handler_lock,
        is_busy=_any_conversation_busy,
        is_tts_active=tts_playback_active,
    )

    # WOR-211 release contract: no detector is constructed and no idle
    # microphone stream is opened. Ctrl+Space enters the same guarded voice
    # workflow intentionally; this does not depend on a missing model file.
    assert not ACOUSTIC_WAKE_AVAILABLE
    print(
        "[wake] acoustic activation unavailable: "
        f"{ACOUSTIC_WAKE_UNAVAILABLE_REASON}. Use Ctrl+Space."
    )
    manual_activation_open = threading.Event()
    manual_activation_open.set()

    def _manual_wake() -> None:
        if not manual_activation_open.is_set():
            print("[wake] manual activation rejected: shutting_down", flush=True)
            return
        if not wake_gate.reserve(0):
            print("[wake] manual activation rejected: busy", flush=True)
            return
        print("[wake] manual activation accepted", flush=True)
        bridge.wake_detected.emit(0)

    stop_hotkey = _start_ctrl_space_listener(_manual_wake)

    def _desktop_busy_message() -> str:
        return "OSIA is still working in this conversation."

    def _is_conversation_busy(conv_id: str | None) -> bool:
        if not conv_id:
            return False
        with busy_lock:
            return conv_id in busy_conversations

    def _set_conversation_busy(conv_id: str | None, busy: bool) -> None:
        if not conv_id:
            return
        with busy_lock:
            if busy:
                busy_conversations.add(conv_id)
            else:
                busy_conversations.discard(conv_id)
            any_busy = bool(busy_conversations)
        bridge.conversation_busy_changed.emit(conv_id, busy)
        bridge.island_state.emit("thinking" if any_busy else "idle")

    def _ensure_active_conversation(seed_text: str | None = None) -> dict:
        nonlocal active_conv
        if active_conv is not None:
            return active_conv
        title = (conversations.smart_title(seed_text)
                 if isinstance(seed_text, str) and seed_text.strip()
                 else None)
        active_conv = conversations.create_conversation(title=title)
        bridge.conversations_changed.emit(active_conv["id"])
        print(f"New conversation: {active_conv['id']}")
        return active_conv

    def _history_for_conversation(conv_id: str) -> list[dict]:
        conv = conversations.get_conversation(conv_id)
        return list((conv or {}).get("messages") or [])

    # ── Agent run helpers ────────────────────────────────────────────────

    def _run_agent(
        user_input,
        conv_id: str,
        history: list[dict],
        *,
        voice_response: bool,
        user_display_text: str | None = None,
        language_text: str | None = None,
        lock_provider: bool = False,
    ) -> None:
        """Run the agent in the current thread, persist + mirror the result.

        Caller has already marked ``conv_id`` busy and echoed the
        user's text into the popup. ``voice_response`` controls whether
        sentences are streamed to TTS as they arrive.
        """
        nonlocal active_conv
        turn_agent = OSIAAgent()
        if lock_provider:
            # Attachment capability was preflighted against this selected path;
            # automatic hierarchy routing must not silently swap it mid-turn.
            turn_agent._disable_hierarchy_for_turn = True
        tts_session_id = start_streaming_session() if voice_response else None
        settings = load_settings()
        selected_model = settings.get("selected_model")
        selected_provider = settings.get("selected_model_provider") or None
        if selected_model:
            try:
                turn_agent.set_model(selected_model, provider=selected_provider)
            except Exception as e:
                print(f"[DEBUG] set_model({selected_model!r}) failed: {e}")

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

        prev_history_len = len(history)

        def on_first_token(*_a, **_kw):
            if voice_response:
                bridge.island_state.emit("speaking")

        def on_stream_sentence(sentence: str) -> None:
            bridge.assistant_stream_chunk.emit(conv_id, sentence)
            if voice_response:
                speak_streaming(
                    sentence,
                    session_id=tts_session_id,
                    language=turn_context.language,
                )

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
            reply = turn_agent.run(
                user_input,
                history=history,
                on_sentence=on_stream_sentence,
                on_first_token=on_first_token,
                current_conversation_id=conv_id,
                turn_context=turn_context,
            )
            if voice_response:
                print(
                    f"[TTS] main.wake_mode: agent.run() returned "
                    f"({len(reply or '')} chars) — sentences were streamed during the run",
                    flush=True,
                )
            _persist_agent_delta(
                conv_id,
                turn_agent,
                prev_history_len,
                user_display_text=user_display_text,
            )
            display = clean_response(reply or "")
            bridge.assistant_stream_final.emit(conv_id, display)
        except Exception as e:
            refusal = _finalize_controlled_refusal(
                e,
                turn_agent,
                conv_id,
                prev_history_len,
                bridge.assistant_stream_final.emit,
                user_display_text=user_display_text,
            )
            if refusal is not None:
                if voice_response:
                    refusal_tts_session = start_streaming_session()
                    speak_streaming(
                        refusal,
                        session_id=refusal_tts_session,
                        language=turn_context.language,
                    )
            else:
                err = localized_text("provider_failure", turn_agent.lang)
                _persist_agent_delta(
                    conv_id,
                    turn_agent,
                    prev_history_len,
                    user_display_text=user_display_text,
                )
                conversations.add_message(conv_id, "assistant", err)
                bridge.assistant_stream_error.emit(conv_id, err)
                if voice_response:
                    error_tts_session = start_streaming_session()
                    speak_streaming(
                        err,
                        session_id=error_tts_session,
                        language=turn_context.language,
                    )
        finally:
            _set_conversation_busy(conv_id, False)
            bridge.conversations_changed.emit(
                active_conv["id"] if active_conv else ""
            )

    # ── Manual activation handler (voice path) ──────────────────────────

    def handle_wake(generation: int):
        if not wake_gate.consume(
            generation,
            is_current=manual_activation_open.is_set() and generation == 0,
        ):
            print("⚠️  Stale or unreserved wake event — ignored.")
            return

        def _finish_wake_turn() -> None:
            # Streaming TTS is asynchronous with respect to agent completion.
            # Hold the handler reservation until every queued sentence finishes.
            wait_for_playback_complete()
            wake_gate.finish()

        # Bloc 4 — Live API voice-to-voice. When enabled, OSIA's local STT
        # (Whisper) transcription and TTS speak steps are bypassed entirely:
        # the provider's live/realtime endpoint streams audio in and speaks
        # back natively. The real WebSocket streaming is a separate task —
        # _connect_live_api() is currently a stub (see osia.voice.live_api).
        try:
            live_settings = load_settings()
        except Exception as exc:
            print(f"[wake] could not load voice settings: {exc}")
            bridge.island_state.emit("idle")
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
                f"[LIVE API] active — STT and TTS bypassed, using {provider} "
                "live endpoint"
            )

            def live_process():
                try:
                    from osia.voice.live_api import _connect_live_api
                    _connect_live_api(provider, model)
                finally:
                    bridge.island_state.emit("idle")
                    _finish_wake_turn()

            threading.Thread(target=live_process, daemon=True).start()
            return

        bridge.island_state.emit("listening")

        def process():
            try:
                # Deferred heavy import (faster_whisper, ~4.3s on Windows). This
                # runs on the wake daemon thread, never on the GUI startup path.
                from osia.voice.stt import listen_for_command
                # Optional spoken greeting before listening. When enabled, OSIA
                # speaks the preconfigured text first, then starts capturing the
                # command (speak() is synchronous, so the greeting finishes
                # before the mic opens — it never bleeds into the recording).
                if live_settings.get("wake_greeting_enabled"):
                    greeting = (live_settings.get("wake_greeting_text") or "").strip()
                    if greeting:
                        bridge.island_state.emit("speaking")
                        speak_greeting_cached(greeting)
                        bridge.island_state.emit("listening")
                user_text = listen_for_command(max_retries=3)
                if not user_text:
                    bridge.island_state.emit("idle")
                    speak("Je n'ai pas compris, répétez.")
                    return
                print(f"📝 You said: {user_text}")
                # WOR-10 — intercept "nouveau chat" / "new chat" before the LLM.
                if is_new_chat_command(user_text):
                    print("🆕 Voice command recognised: new chat")
                    bridge.new_chat.emit()        # reset agent + GUI (Qt thread)
                    _notify_mobile_new_chat()      # push {"type":"new_chat"} to mobile
                    bridge.island_state.emit("speaking")
                    speak(i18n.t("conversations.new_chat_confirm"))
                    bridge.island_state.emit("idle")
                    return
                conv = _ensure_active_conversation(user_text)
                conv_id = conv["id"]
                if _is_conversation_busy(conv_id):
                    bridge.island_state.emit("speaking")
                    speak(_desktop_busy_message())
                    bridge.island_state.emit("idle")
                    return
                history = _history_for_conversation(conv_id)
                _set_conversation_busy(conv_id, True)
                bridge.chat_message_for_conversation.emit(conv_id, "user", user_text)
                _run_agent(user_text, conv_id, history, voice_response=True)
            finally:
                _finish_wake_turn()

        threading.Thread(target=process, daemon=True).start()

    # ── Text handler (typed message in popup) ────────────────────────────

    def _send_user_input(
        user_input,
        display_text: str,
        *,
        attachment_turn: bool = False,
        language_text: str | None = None,
    ) -> bool:
        conv = _ensure_active_conversation(display_text)
        conv_id = conv["id"]
        if attachment_turn:
            popup.bind_unbound_draft(conv_id)
        if _is_conversation_busy(conv_id):
            popup.show_conversation_busy_notice()
            print("Send fired while this conversation is busy; rejected.")
            return False
        tts_stop()
        history = _history_for_conversation(conv_id)
        _set_conversation_busy(conv_id, True)
        bridge.chat_message_for_conversation.emit(conv_id, "user", display_text)

        def process():
            _run_agent(
                user_input,
                conv_id,
                history,
                voice_response=False,
                user_display_text=display_text,
                language_text=language_text,
                lock_provider=attachment_turn,
            )

        threading.Thread(target=process, daemon=True).start()
        return True

    def handle_send_text(text: str):
        _send_user_input(text, text)

    # ── Popup chat / message wiring ─────────────────────────────────────

    def handle_chat_message(role: str, text: str):
        popup.append_message(role, text)

    def handle_chat_message_for_conversation(conv_id: str, role: str, text: str):
        if active_conv and active_conv.get("id") == conv_id:
            popup.append_message(role, text)

    def handle_assistant_stream_chunk(conv_id: str, text: str):
        if active_conv and active_conv.get("id") == conv_id:
            popup.append_assistant_stream_chunk(text)

    def handle_assistant_stream_final(conv_id: str, text: str):
        if active_conv and active_conv.get("id") == conv_id:
            popup.finalize_assistant_stream(text)

    def handle_assistant_stream_error(conv_id: str, text: str):
        if active_conv and active_conv.get("id") == conv_id:
            popup.fail_assistant_stream(text)

    def handle_conversation_busy_changed(conv_id: str, busy: bool):
        popup.set_conversation_busy(conv_id, busy)

    def handle_island_state(state: str):
        island.set_state(state)

    def handle_autonomous_progress(payload: dict):
        # GUI thread (queued from the Bridge signal) — safe to touch widgets.
        popup.update_autonomous_progress(payload)

    # ── Conversation management ──────────────────────────────────────────

    def handle_new_conversation():
        nonlocal active_conv
        tts_stop()
        agent.reset()
        active_conv = conversations.create_conversation()
        popup.set_messages([])
        popup.refresh_conversations(active_conv["id"])
        print(f"🆕 New conversation: {active_conv['id']}")

    def handle_voice_new_chat():
        # GUI-thread reset for the "nouveau chat" voice command (WOR-10).
        # Same effect as clicking "Nouveau chat": clear agent history, start a
        # fresh conversation, and reset the chat view + Dynamic Island.
        nonlocal active_conv
        tts_stop()
        agent.reset()
        active_conv = conversations.create_conversation()
        popup.reset_conversation()
        popup.refresh_conversations(active_conv["id"])
        island.set_state("idle")
        print(f"🆕 New conversation (voice): {active_conv['id']}")

    def handle_open_conversation(conv_id: str):
        nonlocal active_conv
        conv = conversations.get_conversation(conv_id)
        if not conv:
            return
        tts_stop()
        agent.reset()
        msgs = conv.get("messages") or []
        # Seed the agent with history so the next turn keeps context.
        agent.messages = [agent.messages[0]] + [
            {"role": m["role"], "content": m.get("content", "")}
            for m in msgs
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        active_conv = conv
        popup.set_messages(msgs)
        popup.refresh_conversations(active_conv["id"])
        print(f"📂 Loaded conversation: {conv_id}")

    def handle_delete_conversation(conv_id: str):
        nonlocal active_conv
        _set_conversation_busy(conv_id, False)
        if active_conv and conv_id == active_conv.get("id"):
            tts_stop()
        ok = conversations.delete_conversation(conv_id)
        if ok:
            popup.discard_conversation_draft(conv_id)
        print(f"🗑  Deleted conversation {conv_id}: {'ok' if ok else 'not found'}")
        if active_conv and conv_id == active_conv.get("id"):
            agent.reset()
            # Do NOT auto-create a replacement — the list may go (and stay)
            # empty. A new conversation is created only when the user clicks
            # "New chat" or sends a first message.
            active_conv = None
            try:
                popup.set_messages([])
            except RuntimeError:
                # A chat widget was already deleted on the Qt side — the
                # popup guards against this too; never crash the handler.
                pass
        popup.refresh_conversations(active_conv["id"] if active_conv else None)

    def handle_rename_conversation(conv_id: str, title: str):
        # WOR-76: PC rename. Persist to disk (trim + non-empty + TITLE_MAX_LEN
        # cap + manual_title flag all enforced in the store) then refresh the PC
        # panel in-process — exactly like PC delete. The store re-reads from disk
        # on the next add_message, so active_conv's stale in-memory title is
        # harmless (only its id is ever consumed).
        ok = conversations.rename_conversation(conv_id, title)
        print(f"✏️  Renamed conversation {conv_id}: {'ok' if ok else 'rejected'}")
        popup.refresh_conversations(active_conv["id"] if active_conv else None)

    # ── Model + settings wiring ─────────────────────────────────────────

    def handle_model_change(model: str, provider: str):
        try:
            agent.set_model(model, provider=provider)
        except Exception as e:
            print(f"⚠️  Could not switch model: {e}")
            return
        set_setting("selected_model", model)
        set_setting("selected_model_provider", provider)
        popup.set_current_model(model, provider)
        print(f"🔄 Model changed to: {model} (provider: {agent.provider})")

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

    # ── Signal wiring ───────────────────────────────────────────────────

    bridge.wake_detected.connect(handle_wake)
    bridge.chat_message.connect(handle_chat_message)
    bridge.chat_message_for_conversation.connect(handle_chat_message_for_conversation)
    bridge.assistant_stream_chunk.connect(handle_assistant_stream_chunk)
    bridge.assistant_stream_final.connect(handle_assistant_stream_final)
    bridge.assistant_stream_error.connect(handle_assistant_stream_error)
    bridge.island_state.connect(handle_island_state)
    bridge.conversation_busy_changed.connect(handle_conversation_busy_changed)
    bridge.autonomous_progress.connect(handle_autonomous_progress)
    bridge.autonomous_resume_ready.connect(popup.show_autonomous)
    bridge.remote_models_signal.connect(handle_remote_models)
    bridge.conversations_changed.connect(popup.refresh_conversations)
    bridge.new_chat.connect(handle_voice_new_chat)
    # WOR-22: Ctrl+Alt+O toggles the popup, keeping the GUI reachable even when
    # the floating capsule is hidden.
    bridge.toggle_popup.connect(popup.toggle)
    stop_popup_hotkey = _start_popup_hotkey_listener(bridge.toggle_popup.emit)

    popup.send_text.connect(handle_send_text)
    popup.submit_draft.connect(handle_submit_draft)
    popup.new_conversation.connect(handle_new_conversation)
    popup.open_conversation.connect(handle_open_conversation)
    popup.rename_conversation.connect(handle_rename_conversation)
    popup.delete_conversation.connect(handle_delete_conversation)
    popup.model_changed.connect(handle_model_change)
    popup.api_keys_saved.connect(handle_keys_saved)
    popup.chatgpt_status_changed.connect(handle_chatgpt_status_changed)
    # WOR-22: the "Show the floating capsule" toggle hides/shows the capsule.
    popup.capsule_visibility_changed.connect(island.set_capsule_visible)

    # Initial population — no active conversation yet; the panel shows the
    # stored list (or its "No conversations" placeholder when empty).
    popup.refresh_conversations(None)
    discover_models_async(
        lambda remote: bridge.remote_models_signal.emit(remote)
    )

    # ── Scheduled tasks + heartbeat (Bloc 5) ─────────────────────────────
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
            print(f"[CRON] busy — scheduled task skipped: {task_text[:60]}")
            return {
                "status": "failed",
                "message": "OSIA was busy; scheduled task skipped",
            }
        conversation_stale = False
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
            bridge.island_state.emit("thinking")
            if conv is not None:
                history = conv.get("messages") or []
                print(f"[CRON] conversation {conversation_id}: loaded "
                      f"{len(history)} messages for context")
                if scheduled_execution is not None:
                    reply = cron_agent.run_scheduled(
                        task_text,
                        scheduled_execution,
                        history=history,
                        current_conversation_id=conversation_id,
                    )
                else:
                    reply = cron_agent.run(
                        task_text,
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
                    )
                else:
                    reply = cron_agent.run(task_text)
            display = clean_response(reply or "")
            if display:
                if conv is not None and conversation_id:
                    bridge.chat_message_for_conversation.emit(
                        conversation_id, "assistant", f"⏰ {display}"
                    )
                    bridge.conversations_changed.emit(
                        active_conv["id"] if active_conv else ""
                    )
                speak(
                    display,
                    language=getattr(
                        getattr(cron_agent, "turn_context", None),
                        "language",
                        None,
                    ),
                )
            if conversation_stale:
                return {
                    "status": "completed",
                    "conversation_stale": True,
                    "message": "linked conversation not found; ran unlinked",
                }
            return {"status": "completed"}
        except Exception as e:
            print(f"[CRON] task error: {e}")
            return {"status": "failed", "message": str(e)}
        finally:
            cron_agent._disable_hierarchy_for_turn = False
            bridge.island_state.emit("idle")
            handler_lock.release()

    def heartbeat_alert(message: str) -> None:
        # Wait briefly for any active turn to finish so the alert isn't spoken
        # over it; if still busy, log rather than drop it silently.
        if not handler_lock.acquire(timeout=20):
            print(f"[HEARTBEAT] busy — alert not spoken: {message}")
            return
        try:
            bridge.chat_message.emit("assistant", f"💓 {message}")
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
        from osia.notifications import notify, safe_task_title

        safe_title = safe_task_title(title)
        if status == "failed":
            body = f"Tâche planifiée échouée: {safe_title}"
        else:
            body = f"Tâche planifiée terminée: {safe_title}"
        notify("OSIA", body)
        _notify_mobile_cron_notification(
            task_id=task_id,
            title=safe_title,
            status=status,
            conversation_id=conversation_id,
        )

    cron_manager = CronManager(run_cron_task, notify_task=notify_cron_task)
    unsubscribe_cron_wake = scheduled_tasks.add_change_callback(cron_manager.wake)
    heartbeat_monitor = HeartbeatMonitor(heartbeat_alert)
    cron_manager.start()
    heartbeat_monitor.start()

    # ── Autonomous agent (WOR-45 Phase 4) ────────────────────────────────
    # A DEDICATED agent (never shared with the chat) drives the orchestrator;
    # the planner + orchestrator + state layer are consumed as black boxes. The
    # engine is created but NOT started — it runs only on an explicit "Start"
    # from the panel, or on a user-confirmed "Resume" at startup (below). The
    # popup signal handlers below reference `orchestrator` / `autonomous_state`,
    # which are bound in this same scope before the Qt event loop starts, so the
    # handlers (only ever invoked via signals after app.exec()) resolve them.
    autonomous_agent = OSIAAgent()
    # Mutable holder for the live task_state dict so the progress closure (run on
    # the orchestrator's daemon thread) and the abort/interrupt workers can read
    # the same reference the orchestrator mutates in place.
    autonomous_state = {"st": None, "mobile_snapshot_key": ""}

    def _is_server_mobile_state(st) -> bool:
        return isinstance(st, dict) and st.get("_server_mobile_run") is True

    def _apply_server_mobile_action(action: str) -> bool:
        result = _send_server_autonomous_pc_action(action)
        if not isinstance(result, dict):
            return False
        st = result.get("state")
        if not isinstance(st, dict):
            return False
        autonomous_state["st"] = st
        bridge.autonomous_progress.emit({
            "event": result.get("event") or (
                "authorization_error" if result.get("ok") is not True else ""
            ),
            "reason_code": result.get("error") or "",
            **st,
        })
        return result.get("ok") is True

    def _orch_snapshot(st) -> dict:
        """Plain-dict snapshot (done/total tasks + current phase name) computed
        from a task_state dict. Pure reads — safe inside progress_cb, which runs
        on the orchestrator thread (the same thread that mutates `st`)."""
        if not isinstance(st, dict):
            return {}
        done, total, phase_name = count_progress(st)
        return {
            "done": done,
            "total": total,
            "phase_name": phase_name,
            "objective": st.get("objective", "") or "",
            "status": st.get("status", "") or "",
            "language": normalize_locale(st.get("language")) or i18n.DEFAULT_LANG,
            "plan_summary": [
                {
                    "name": str(phase.get("name") or ""),
                    "tasks": [
                        str(task.get("objective") or "")
                        for task in phase.get("tasks") or []
                        if isinstance(task, dict)
                    ],
                }
                for phase in st.get("phases") or []
                if isinstance(phase, dict)
            ],
            **autonomous_authorization.pc_projection(st),
            **autonomous_diagnostics.projection(st),
        }

    def _on_orch_progress(payload):
        # Runs on the orchestrator daemon thread. ONLY compute a plain-dict
        # snapshot and emit via the Bridge — NEVER touch a Qt widget here.
        snap = _orch_snapshot(autonomous_state.get("st"))
        bridge.autonomous_progress.emit({**payload, **snap})

    orchestrator = Orchestrator(autonomous_agent, progress_cb=_on_orch_progress)

    def handle_autonomous_start(objective: str, provider: str, model: str):
        # Plan OFF the GUI thread (create_plan_state blocks on an LLM run), then
        # persist + start the orchestrator. Mirrors the chat worker pattern.
        def _plan_and_run():
            autonomous_context = resolve_turn_context(
                objective,
                saved_language=i18n.get_language(),
                reference="desktop-autonomous",
            )
            autonomous_agent.set_turn_context(autonomous_context)
            resolution = resolve_current_autonomous_selection(
                provider_id=provider,
                model_id=model,
                refresh_account=True,
            )
            resolved_provider = resolution.get("provider_id") or ""
            resolved_model = resolution.get("model_id") or ""
            if resolution.get("fallback_occurred"):
                print(
                    "[WOR-182] Autonomous model fallback: "
                    f"{resolution.get('reason') or 'unavailable'}"
                )
            model_setup_failed = False
            if resolved_model and resolved_provider:
                try:
                    apply_autonomous_agent_model(
                        autonomous_agent, resolved_provider, resolved_model
                    )
                except Exception:
                    model_setup_failed = True
                    autonomous_diagnostics.emit_diagnostic_log(
                        autonomous_diagnostics.make_diagnostic(
                            None,
                            stage=autonomous_diagnostics.AutonomousStage.PLANNING,
                            category=(
                                autonomous_diagnostics.FailureCategory
                                .PLANNING_PROVIDER_FAILURE
                            ),
                            reason_code="model_setup_failure",
                            provider=resolved_provider,
                            model=resolved_model,
                            resumable=False,
                            checkpoint_present=False,
                        )
                    )
            if resolved_provider and resolved_model and not model_setup_failed:
                actual_provider, actual_model = (
                    autonomous_diagnostics.actual_provider_model(
                        autonomous_agent, resolved_provider, resolved_model,
                    )
                )
            else:
                actual_provider, actual_model = "", ""
            planning_failures = []
            if resolved_provider and resolved_model and not model_setup_failed:
                try:
                    st = planner.create_plan_state(
                        objective, actual_provider, actual_model, autonomous_agent,
                        failure_cb=planning_failures.append,
                        language=autonomous_context.language,
                        turn_context=autonomous_context,
                        origin_ref="desktop",
                    )
                except Exception:
                    st = None
            else:
                st = None
            if not st:
                if planning_failures:
                    failure = planning_failures[-1]
                    category = failure.error_category
                    reason_code = failure.reason
                else:
                    category = (
                        autonomous_diagnostics.FailureCategory
                        .PLANNING_PROVIDER_FAILURE
                    )
                    if model_setup_failed:
                        reason_code = "model_setup_failure"
                    elif not resolved_provider or not resolved_model:
                        reason_code = "no_configured_model"
                    else:
                        reason_code = "planning_internal_failure"
                diagnostic = autonomous_diagnostics.make_diagnostic(
                    None,
                    stage=autonomous_diagnostics.AutonomousStage.PLANNING,
                    category=category,
                    reason_code=reason_code,
                    provider=actual_provider,
                    model=actual_model,
                    resumable=False,
                    checkpoint_present=True,
                )
                failed_state = new_failed_task_state(
                    actual_provider, actual_model, diagnostic,
                    language=autonomous_context.language,
                )
                try:
                    save_task_state(failed_state)
                except Exception:
                    autonomous_diagnostics.set_checkpoint_present(
                        failed_state, False,
                    )
                autonomous_diagnostics.emit_diagnostic_log(
                    failed_state.get("failure_diagnostic")
                )
                autonomous_state["st"] = failed_state
                bridge.autonomous_progress.emit(
                    {"event": "plan_failed", **_orch_snapshot(failed_state)}
                )
                return
            try:
                save_task_state(st)
            except Exception:
                st["status"] = "failed"
                diagnostic = autonomous_diagnostics.make_diagnostic(
                    st,
                    stage=autonomous_diagnostics.AutonomousStage.PERSISTENCE,
                    category=(
                        autonomous_diagnostics.FailureCategory.CHECKPOINT_FAILURE
                    ),
                    reason_code="checkpoint_failure",
                    provider=actual_provider,
                    model=actual_model,
                    resumable=False,
                    checkpoint_present=False,
                )
                autonomous_diagnostics.record_diagnostic(st, diagnostic)
                autonomous_state["st"] = st
                bridge.autonomous_progress.emit(
                    {"event": "failed", **_orch_snapshot(st)}
                )
                return
            autonomous_state["st"] = st
            if st.get("run_state") != "ready":
                bridge.autonomous_progress.emit({
                    "event": "autonomous_plan_ready",
                    **_orch_snapshot(st),
                })
                return
            try:
                orchestrator.start(st)
            except Exception:
                st["status"] = "failed"
                diagnostic = autonomous_diagnostics.make_diagnostic(
                    st,
                    stage=autonomous_diagnostics.AutonomousStage.ORCHESTRATOR,
                    category=(
                        autonomous_diagnostics.FailureCategory.ORCHESTRATOR_FAILURE
                    ),
                    reason_code="start_worker_failure",
                    provider=actual_provider,
                    model=actual_model,
                    resumable=False,
                    checkpoint_present=True,
                )
                autonomous_diagnostics.record_diagnostic(st, diagnostic)
                try:
                    save_task_state(st)
                except Exception:
                    autonomous_diagnostics.set_checkpoint_present(st, False)
                bridge.autonomous_progress.emit(
                    {"event": "failed", **_orch_snapshot(st)}
                )
                return
            # Flip the panel "Planning…" → "Running…" with the first snapshot, and
            # ONLY now (after start() has spawned the loop) — this is what enables
            # the panel's Interrupt/Abort, so a cancel can never race a start() that
            # would clear the stop signal. The orchestrator emits its own events
            # only on task transitions, never on start, so this won't double-fire.
            bridge.autonomous_progress.emit({"event": "running", **_orch_snapshot(st)})

        threading.Thread(target=_plan_and_run, daemon=True).start()

    def _ensure_autonomous_agent_for_state(st: dict) -> bool:
        provider = str(st.get("provider_id") or st.get("provider") or "")
        model = str(st.get("model_id") or st.get("model") or "")
        if not provider or not model:
            return False
        try:
            apply_autonomous_agent_model(autonomous_agent, provider, model)
        except Exception:
            return False
        actual_provider, actual_model = autonomous_diagnostics.actual_provider_model(
            autonomous_agent, provider, model,
        )
        return actual_provider == provider and actual_model == model

    def handle_autonomous_authorize():
        def _authorize():
            st = autonomous_state.get("st")
            if not isinstance(st, dict):
                return
            if _is_server_mobile_state(st):
                if not _apply_server_mobile_action("authorize"):
                    bridge.autonomous_progress.emit({
                        "event": "authorization_error",
                        "reason_code": "server_authorization_failure",
                        **st,
                    })
                return
            try:
                if not _ensure_autonomous_agent_for_state(st):
                    raise autonomous_authorization.AuthorizationError(
                        "model_identity_unavailable"
                    )
                response = autonomous_authorization.response_for_current_request(
                    st, action="approve", origin_ref="desktop",
                )
                autonomous_authorization.authorize_and_start(
                    st, response, origin_ref="desktop",
                )
                save_task_state(st)
                orchestrator.start(st)
            except autonomous_authorization.AuthorizationError as exc:
                bridge.autonomous_progress.emit({
                    "event": "authorization_error",
                    "reason_code": exc.code,
                    **_orch_snapshot(st),
                })
                return
            except Exception:
                bridge.autonomous_progress.emit({
                    "event": "authorization_error",
                    "reason_code": "authorization_start_failure",
                    **_orch_snapshot(st),
                })
                return
            bridge.autonomous_progress.emit({"event": "running", **_orch_snapshot(st)})

        threading.Thread(target=_authorize, daemon=True).start()

    def handle_autonomous_scope_extension(action: str):
        def _respond():
            st = autonomous_state.get("st")
            if not isinstance(st, dict):
                return
            if _is_server_mobile_state(st):
                mapped = {
                    "approve_exact": "scope_approve",
                    "deny": "scope_deny",
                    "cancel": "cancel",
                }.get(action)
                if not mapped or not _apply_server_mobile_action(mapped):
                    bridge.autonomous_progress.emit({
                        "event": "authorization_error",
                        "reason_code": "server_scope_extension_failure",
                        **st,
                    })
                return
            try:
                if action == "approve_exact" and not _ensure_autonomous_agent_for_state(st):
                    raise autonomous_authorization.AuthorizationError(
                        "model_identity_unavailable"
                    )
                response = autonomous_authorization.response_for_current_request(
                    st, action=action, origin_ref="desktop",
                )
                autonomous_authorization.approve_scope_extension(
                    st, response, origin_ref="desktop",
                )
                save_task_state(st)
                if action == "approve_exact":
                    orchestrator.resume(st)
                    event = "running"
                else:
                    event = "scope_extension_denied"
            except autonomous_authorization.AuthorizationError as exc:
                bridge.autonomous_progress.emit({
                    "event": "authorization_error",
                    "reason_code": exc.code,
                    **_orch_snapshot(st),
                })
                return
            except Exception:
                bridge.autonomous_progress.emit({
                    "event": "authorization_error",
                    "reason_code": "scope_extension_failure",
                    **_orch_snapshot(st),
                })
                return
            bridge.autonomous_progress.emit({"event": event, **_orch_snapshot(st)})

        threading.Thread(target=_respond, daemon=True).start()

    def handle_autonomous_revoke():
        def _revoke():
            st = autonomous_state.get("st")
            if not isinstance(st, dict):
                return
            if _is_server_mobile_state(st):
                if not _apply_server_mobile_action("revoke"):
                    bridge.autonomous_progress.emit({
                        "event": "authorization_error",
                        "reason_code": "server_revocation_failure",
                        **st,
                    })
                return
            try:
                autonomous_authorization.revoke_run_authorization(st)
                orchestrator.stop()
                save_task_state(st)
            except Exception:
                bridge.autonomous_progress.emit({
                    "event": "authorization_error",
                    "reason_code": "revocation_failure",
                    **_orch_snapshot(st),
                })
                return
            bridge.autonomous_progress.emit({
                "event": "autonomous_authorization_revoked",
                **_orch_snapshot(st),
            })

        threading.Thread(target=_revoke, daemon=True).start()

    def handle_autonomous_interrupt():
        # Pause: stop() joins ≤2s, so run it off the GUI thread. The persisted
        # state stays "in_progress" → the task is resumable on the next launch.
        def _pause():
            st = autonomous_state.get("st")
            try:
                orchestrator.stop()
            except Exception:
                autonomous_diagnostics.emit_diagnostic_log(
                    autonomous_diagnostics.make_diagnostic(
                        st,
                        stage=autonomous_diagnostics.AutonomousStage.CONTROL,
                        category=(
                            autonomous_diagnostics.FailureCategory
                            .ORCHESTRATOR_FAILURE
                        ),
                        reason_code="interrupt_stop_failure",
                        resumable=resume_state_is_valid(st),
                        checkpoint_present=True,
                    )
                )
            if isinstance(st, dict):
                provider_now, model_now = autonomous_diagnostics.actual_provider_model(
                    autonomous_agent, st.get("provider", ""), st.get("model", ""),
                )
                diagnostic = autonomous_diagnostics.make_diagnostic(
                    st,
                    stage=autonomous_diagnostics.AutonomousStage.CONTROL,
                    category=autonomous_diagnostics.FailureCategory.INTERRUPTED,
                    reason_code="user_interrupted",
                    provider=provider_now,
                    model=model_now,
                    resumable=resume_state_is_valid(st),
                    checkpoint_present=True,
                )
                autonomous_diagnostics.record_diagnostic(st, diagnostic)
                try:
                    save_task_state(st)
                except Exception:
                    autonomous_diagnostics.set_checkpoint_present(st, False)
            bridge.autonomous_progress.emit(
                {"event": "interrupted", **_orch_snapshot(st)}
            )

        threading.Thread(target=_pause, daemon=True).start()

    def handle_autonomous_abort():
        # Discard. Mark the SHARED state dict aborted FIRST — before stop() — so
        # that if the engine daemon outlives stop()'s 2s join (mid agent.run) and
        # checkpoints once more on its way out, it persists status="aborted"
        # (never "in_progress"), which _offer_resume_if_pending ignores. Then stop
        # the loop and delete the checkpoint (a late daemon re-save would only
        # re-create it as "aborted", still not resumable).
        def _abort():
            st = autonomous_state.get("st")
            if _is_server_mobile_state(st):
                if not _apply_server_mobile_action("cancel"):
                    bridge.autonomous_progress.emit({
                        "event": "authorization_error",
                        "reason_code": "server_cancel_failure",
                        **st,
                    })
                return
            if isinstance(st, dict):
                st["status"] = "aborted"
                st["run_state"] = "cancelled"
                try:
                    autonomous_authorization.revoke_run_authorization(st)
                    st["status"] = "aborted"
                    st["run_state"] = "cancelled"
                except Exception:
                    pass
                provider_now, model_now = autonomous_diagnostics.actual_provider_model(
                    autonomous_agent, st.get("provider", ""), st.get("model", ""),
                )
                diagnostic = autonomous_diagnostics.make_diagnostic(
                    st,
                    stage=autonomous_diagnostics.AutonomousStage.CONTROL,
                    category=autonomous_diagnostics.FailureCategory.ABORTED,
                    reason_code="user_aborted",
                    provider=provider_now,
                    model=model_now,
                    resumable=False,
                    checkpoint_present=False,
                )
                autonomous_diagnostics.record_diagnostic(st, diagnostic)
            try:
                orchestrator.stop()
            except Exception:
                autonomous_diagnostics.emit_diagnostic_log(
                    autonomous_diagnostics.make_diagnostic(
                        st,
                        stage=autonomous_diagnostics.AutonomousStage.CONTROL,
                        category=(
                            autonomous_diagnostics.FailureCategory
                            .ORCHESTRATOR_FAILURE
                        ),
                        reason_code="abort_stop_failure",
                        resumable=False,
                        checkpoint_present=False,
                    )
                )
            clear_task_state()
            bridge.autonomous_progress.emit({"event": "aborted", **_orch_snapshot(st)})

        threading.Thread(target=_abort, daemon=True).start()

    def _poll_server_mobile_authorization() -> bool:
        """Mirror the canonical server run into the local PC panel."""

        st = _load_server_autonomous_pc_state()
        if not isinstance(st, dict) or not st.get("run_id"):
            return False
        current = autonomous_state.get("st")
        same_mobile_run = (
            _is_server_mobile_state(current)
            and current.get("run_id") == st.get("run_id")
        )
        run_state = str(st.get("run_state") or "")
        if run_state in {"completed", "failed", "cancelled"} and not same_mobile_run:
            return False
        key = "|".join((
            str(st.get("run_id") or ""),
            str(st.get("updated_at") or ""),
            run_state,
            str(st.get("plan_revision") or ""),
            str(st.get("authorization_revision") or ""),
            str((st.get("pending_scope_extension") or {}).get("request_id") or ""),
            str((st.get("pending_scope_extension") or {}).get("responded") or ""),
        ))
        if key == autonomous_state.get("mobile_snapshot_key"):
            return True
        autonomous_state["mobile_snapshot_key"] = key
        autonomous_state["st"] = st
        event = {
            "authorization_pending": "autonomous_plan_ready",
            "ready": "running",
            "running": "running",
            "suspended_scope_extension": "suspended_scope_extension",
            "suspended_authorization_revoked": "suspended_authorization_revoked",
            "blocked_outcome_unknown": "blocked_outcome_unknown",
            "completed": "done",
            "failed": "failed",
            "cancelled": "aborted",
        }.get(run_state, run_state)
        if not same_mobile_run:
            popup.show_autonomous(st)
        else:
            popup.update_autonomous_progress({"event": event, **st})
        return True

    popup.autonomous_start_requested.connect(handle_autonomous_start)
    popup.autonomous_authorize_requested.connect(handle_autonomous_authorize)
    popup.autonomous_scope_extension_response.connect(
        handle_autonomous_scope_extension
    )
    popup.autonomous_revoke_requested.connect(handle_autonomous_revoke)
    popup.autonomous_interrupt_requested.connect(handle_autonomous_interrupt)
    popup.autonomous_abort_requested.connect(handle_autonomous_abort)

    mobile_authorization_timer = QTimer()
    mobile_authorization_timer.setInterval(1000)
    mobile_authorization_timer.timeout.connect(_poll_server_mobile_authorization)
    mobile_authorization_timer.start()

    trigger_label = "Press Ctrl+Space to activate"

    print("=" * 60)
    print("  OSIA — Wake Mode (Dynamic Island)")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print(f"  {trigger_label}")
    print("  Ctrl+Alt+O: show/hide the OSIA window")
    print("=" * 60)

    def on_quit():
        # Stop the Bloc 5 background threads cleanly before the app tears down.
        tts_shutdown()
        cron_manager.stop()
        unsubscribe_cron_wake()
        heartbeat_monitor.stop()
        # WOR-45 Phase 4 — stop the autonomous loop too (safe if never started).
        orchestrator.stop()
        manual_activation_open.clear()
        wake_gate.cancel(0)
        if stop_hotkey:
            stop_hotkey()
        if stop_popup_hotkey:
            stop_popup_hotkey()
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
            from osia.tools.browser import close_browser
            close_browser()
        except Exception:
            pass

    app.aboutToQuit.connect(on_quit)

    # ── WOR-45 Phase 1: resume a previously interrupted autonomous task ──
    # DETECTION ONLY (non-blocking). If a persisted autonomous task is still
    # "in_progress" — the process died / was restarted mid-run — offer the user
    # Resume / Abort once the event loop is live. Phase 1 has no orchestrator
    # yet, so "Resume" only LOGS what it *would* do; "Abort" marks the task
    # aborted and atomically checkpoints it. The whole check is deferred via
    # QTimer.singleShot(0, ...) so it NEVER blocks GUI startup (the modal prompt
    # only appears after app.exec() has started the event loop).
    def _offer_resume_if_pending() -> None:
        if _poll_server_mobile_authorization():
            return
        try:
            from osia.autonomous.state import load_task_state, save_task_state
        except Exception:
            print("[WOR-45] resume check skipped (import failed)")
            return
        state = load_task_state()
        if not isinstance(state, dict) or state.get("status") != "in_progress":
            return  # no pending task — normal startup
        try:
            autonomous_authorization.revalidate_loaded_authorization(
                state, origin_ref="desktop",
            )
            save_task_state(state)
        except Exception:
            state["run_state"] = "authorization_pending"
            state["authorization_status"] = "corrupt"
            state["grant_refs"] = []
        if state.get("run_state") in {
            "authorization_pending",
            "suspended_scope_extension",
            "suspended_authorization_revoked",
            "blocked_outcome_unknown",
        }:
            autonomous_state["st"] = state
            popup.show_autonomous(state)
            bridge.autonomous_progress.emit({
                "event": (
                    "autonomous_plan_ready"
                    if state.get("run_state") == "authorization_pending"
                    else state.get("run_state")
                ),
                **_orch_snapshot(state),
            })
            return
        if not resume_state_is_valid(state):
            state["status"] = "failed"
            diagnostic = autonomous_diagnostics.make_diagnostic(
                state,
                stage=autonomous_diagnostics.AutonomousStage.RESUME,
                category=(
                    autonomous_diagnostics.FailureCategory.INVALID_RESUME_STATE
                ),
                reason_code="invalid_resume_state",
                resumable=False,
                checkpoint_present=True,
            )
            autonomous_diagnostics.record_diagnostic(state, diagnostic)
            try:
                save_task_state(state)
            except Exception:
                autonomous_diagnostics.set_checkpoint_present(state, False)
            autonomous_state["st"] = state
            popup.show_autonomous(state)
            bridge.autonomous_progress.emit(
                {"event": "failed", **_orch_snapshot(state)}
            )
            return
        task_id = state.get("task_id", "?")
        cursor = state.get("cursor") or {}
        phase = cursor.get("phase", 0)
        task = cursor.get("task", 0)
        objective = (state.get("objective") or "")[:200]
        from PyQt6.QtWidgets import QMessageBox
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("OSIA")
        box.setText("Une tâche autonome a été interrompue.")
        box.setInformativeText(
            (f"« {objective} »\n\n" if objective else "")
            + f"Reprendre là où elle s'est arrêtée (phase {phase}, tâche {task}) ?"
        )
        resume_btn = box.addButton("Reprendre", QMessageBox.ButtonRole.AcceptRole)
        abort_btn = box.addButton("Abandonner", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(resume_btn)
        box.exec()
        if box.clickedButton() is abort_btn:
            state["status"] = "aborted"
            diagnostic = autonomous_diagnostics.make_diagnostic(
                state,
                stage=autonomous_diagnostics.AutonomousStage.CONTROL,
                category=autonomous_diagnostics.FailureCategory.ABORTED,
                reason_code="user_aborted",
                resumable=False,
                checkpoint_present=True,
            )
            autonomous_diagnostics.record_diagnostic(state, diagnostic)
            try:
                save_task_state(state)
            except Exception:
                autonomous_diagnostics.set_checkpoint_present(state, False)
        else:
            def _resume_pending() -> None:
                resume_resolution = resolve_current_autonomous_selection(
                    provider_id=state.get("provider", ""),
                    model_id=state.get("model", ""),
                    refresh_account=True,
                )
                resume_provider = resume_resolution.get("provider_id") or ""
                resume_model = resume_resolution.get("model_id") or ""
                if resume_resolution.get("fallback_occurred"):
                    print(
                        "[WOR-182] Autonomous resume model fallback: "
                        f"{resume_resolution.get('reason') or 'unavailable'}"
                    )
                resume_context = fixed_turn_context(
                    state.get("language"),
                    source="autonomous_resume",
                    reference=str(state.get("task_id") or "autonomous"),
                    mode=str(state.get("language_mode") or "single-language"),
                    allowed_languages=state.get("allowed_languages") or (),
                    source_language=state.get("source_language"),
                    target_language=state.get("target_language"),
                )
                autonomous_agent.set_turn_context(resume_context)
                if not resume_provider or not resume_model:
                    state["status"] = "failed"
                    diagnostic = autonomous_diagnostics.make_diagnostic(
                        state,
                        stage=autonomous_diagnostics.AutonomousStage.RESUME,
                        category=(
                            autonomous_diagnostics.FailureCategory
                            .PLANNING_PROVIDER_FAILURE
                        ),
                        reason_code="no_configured_model",
                        resumable=False,
                        checkpoint_present=True,
                    )
                    autonomous_diagnostics.record_diagnostic(state, diagnostic)
                    try:
                        save_task_state(state)
                    except Exception:
                        autonomous_diagnostics.set_checkpoint_present(state, False)
                    autonomous_state["st"] = state
                    bridge.autonomous_resume_ready.emit(state)
                    bridge.autonomous_progress.emit(
                        {"event": "failed", **_orch_snapshot(state)}
                    )
                    return
                try:
                    apply_autonomous_agent_model(
                        autonomous_agent, resume_provider, resume_model
                    )
                except Exception:
                    state["status"] = "failed"
                    diagnostic = autonomous_diagnostics.make_diagnostic(
                        state,
                        stage=autonomous_diagnostics.AutonomousStage.RESUME,
                        category=(
                            autonomous_diagnostics.FailureCategory
                            .PLANNING_PROVIDER_FAILURE
                        ),
                        reason_code="model_setup_failure",
                        provider=resume_provider,
                        model=resume_model,
                        resumable=False,
                        checkpoint_present=True,
                    )
                    autonomous_diagnostics.record_diagnostic(state, diagnostic)
                    try:
                        save_task_state(state)
                    except Exception:
                        autonomous_diagnostics.set_checkpoint_present(state, False)
                    autonomous_state["st"] = state
                    bridge.autonomous_resume_ready.emit(state)
                    bridge.autonomous_progress.emit(
                        {"event": "failed", **_orch_snapshot(state)}
                    )
                    return
                provider_now, model_now = (
                    autonomous_diagnostics.actual_provider_model(
                        autonomous_agent, resume_provider, resume_model,
                    )
                )
                state["provider"] = provider_now
                state["model"] = model_now
                try:
                    save_task_state(state)
                except Exception:
                    autonomous_diagnostics.set_checkpoint_present(state, False)
                autonomous_state["st"] = state
                bridge.autonomous_resume_ready.emit(state)
                try:
                    orchestrator.resume(state)
                except Exception:
                    state["status"] = "failed"
                    diagnostic = autonomous_diagnostics.make_diagnostic(
                        state,
                        stage=autonomous_diagnostics.AutonomousStage.RESUME,
                        category=(
                            autonomous_diagnostics.FailureCategory
                            .ORCHESTRATOR_FAILURE
                        ),
                        reason_code="resume_worker_failure",
                        provider=provider_now,
                        model=model_now,
                        resumable=False,
                        checkpoint_present=True,
                    )
                    autonomous_diagnostics.record_diagnostic(state, diagnostic)
                    try:
                        save_task_state(state)
                    except Exception:
                        autonomous_diagnostics.set_checkpoint_present(state, False)
                    bridge.autonomous_progress.emit(
                        {"event": "failed", **_orch_snapshot(state)}
                    )

            def _resume_pending_safe() -> None:
                try:
                    _resume_pending()
                except Exception:
                    state["status"] = "failed"
                    diagnostic = autonomous_diagnostics.make_diagnostic(
                        state,
                        stage=autonomous_diagnostics.AutonomousStage.RESUME,
                        category=(
                            autonomous_diagnostics.FailureCategory
                            .ORCHESTRATOR_FAILURE
                        ),
                        reason_code="resume_worker_failure",
                        resumable=False,
                        checkpoint_present=True,
                    )
                    autonomous_diagnostics.record_diagnostic(state, diagnostic)
                    try:
                        save_task_state(state)
                    except Exception:
                        autonomous_diagnostics.set_checkpoint_present(state, False)
                    autonomous_state["st"] = state
                    bridge.autonomous_resume_ready.emit(state)
                    bridge.autonomous_progress.emit(
                        {"event": "failed", **_orch_snapshot(state)}
                    )

            threading.Thread(target=_resume_pending_safe, daemon=True).start()

    QTimer.singleShot(0, _offer_resume_if_pending)

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

    # First-launch config bootstrap: create any missing config files
    # (settings.json, mobile pairing token) BEFORE anything starts the mobile
    # WebSocket server, so a fresh install works out of the box on any OS and the
    # server authenticates against a persisted token. On Windows/Mac the GUI is
    # the de-facto sole/primary process, so it creates the token here (on Windows
    # this runs before _ensure_mobile_server_windows launches the server). On
    # Linux a separate systemd server process (run_server.py) is the single token
    # creator, so pass create_token=False to avoid two processes racing to
    # persist divergent tokens. settings.json is always created (GUI is its only
    # creator). Idempotent + best-effort — it must never block startup.
    try:
        from osia.config import ensure_config_files
        ensure_config_files(create_token=not sys.platform.startswith("linux"))
    except Exception as e:
        print(f"[CONFIG] ensure_config_files skipped: {e}")

    # One-time tool health check: logs which tool categories (bash, file
    # read, browser) are usable so missing dependencies show up at startup
    # instead of failing mid-conversation. It imports playwright and probes
    # PATH, so it runs on a daemon thread to keep it off the GUI startup path
    # (WOR perf fix) — it must never block startup.
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
