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

from osia.agent import OSIAAgent, DEFAULT_LANG, get_system_prompt
from osia import conversations
from osia.tools import verify_tools

# Loopback trigger for the mobile WebSocket server (osia/server/server.py,
# listens on :8765). Used by the "nouveau chat" voice command (WOR-10) to push
# a {"type": "new_chat"} reset to every connected device. The endpoint is
# localhost-only on the server side.
_SERVER_NEW_CHAT_URL = "http://127.0.0.1:8765/internal/new_chat"


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


def _persist_agent_delta(conv_id, agent, prev_history_len):
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
        for m in new_msgs:
            conversations.add_message(
                conv_id, m.get("role"), m.get("content", ""),
            )
    except Exception as e:
        print(f"[OSIA] failed to persist conversation: {e}")


def _persist_exchange(conv_id, user_text, assistant_text):
    """Persist a stateless single-turn exchange (used by voice_mode,
    which currently calls run_agent() without keeping history).
    """
    if not conv_id:
        return
    try:
        conversations.add_message(conv_id, "user", user_text)
        conversations.add_message(conv_id, "assistant", assistant_text or "")
    except Exception as e:
        print(f"[OSIA] failed to persist conversation: {e}")


# ── Ctrl+Space hotkey fallback ──────────────────────────────────────────────

def _start_ctrl_space_listener(callback):
    """
    Start a global keyboard listener that triggers `callback` on Ctrl+Space.
    Used as fallback when no openWakeWord verifier model is available.

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
        except Exception:
            pass

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
            response = agent.run(user_input)
            print(f"\nOSIA: {response}")
            _persist_agent_delta(active_conv["id"], agent, prev_history_len)
        except Exception as e:
            print(f"\n[ERROR] {e}")


def voice_mode():
    """Single-shot voice mode: listen → agent → speak."""
    from osia.voice.stt import listen
    from osia.voice.tts import speak
    from osia.agent import run_agent, clean_response

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
        response = run_agent(user_text)
        response = clean_response(response)
        print(f"💬 OSIA: {response}")

        _persist_exchange(active_conv["id"], user_text, response)
        print(f"[TTS] main.voice_mode: calling speak() ({len(response)} chars)", flush=True)
        speak(response)
        print("[TTS] main.voice_mode: speak() returned", flush=True)


def wake_mode():
    """Wake word mode: continuous listening for wake word, driven by the
    Dynamic Island GUI. Falls back to Ctrl+Space if no verifier model is
    available.
    """
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QObject, pyqtSignal, Qt
    from PyQt6.QtGui import QGuiApplication, QIcon

    from osia.perf import perf_mark
    perf_mark("wake_mode: enter")

    from osia.utils.assets import ICON_PATH, ensure_desktop_entry
    from osia.gui.dynamic_island import DynamicIsland
    from osia.gui.main_popup import MainPopup
    from osia.voice.wake import WakeWordDetector, verifier_available
    # NOTE: osia.voice.stt (faster_whisper, ~4.3s to import on Windows) is NOT
    # imported here. listen_for_command is only used on the wake daemon thread —
    # it is imported lazily inside handle_wake()'s process() worker so the heavy
    # import never blocks GUI startup (WOR perf fix).
    from osia.voice.commands import is_new_chat_command
    from osia.voice.tts import (
        speak, speak_greeting_cached, speak_streaming, stop as tts_stop,
    )
    from osia.agent import clean_response
    from osia import i18n
    from osia.config import load_settings, set_setting
    from osia.models import discover_models_async
    from osia.cron import CronManager
    from osia.heartbeat import HeartbeatMonitor
    perf_mark("wake_mode: heavy imports done")

    class Bridge(QObject):
        wake_detected = pyqtSignal()
        remote_models_signal = pyqtSignal(list)
        # Update the chat with a (role, text) pair from a worker thread.
        chat_message = pyqtSignal(str, str)
        # Drive the island's visual state from a worker thread.
        island_state = pyqtSignal(str)
        # Refresh the conversations panel (new active id) from a worker thread.
        conversations_changed = pyqtSignal(str)
        # Reset chat (agent + GUI) from a worker thread — "nouveau chat" voice
        # command (WOR-10). Runs on the GUI thread so Qt calls are safe.
        new_chat = pyqtSignal()
        # Toggle the popup from the global Ctrl+Alt+O hotkey thread (WOR-22).
        # Marshalled onto the GUI thread so popup.toggle() is Qt-safe.
        toggle_popup = pyqtSignal()

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
    if initial_model:
        try:
            agent.set_model(initial_model)
        except Exception as e:
            print(f"[DEBUG] set_model({initial_model!r}) failed: {e}")
    popup.set_current_model(agent.model)

    # WOR-22: honour the saved "show the floating capsule" preference at launch.
    island.set_capsule_visible(settings.get("show_capsule", True))

    # --- Wake word / hotkey trigger ---
    def on_wake():
        bridge.wake_detected.emit()

    detector = None
    stop_hotkey = None
    if verifier_available():
        detector = WakeWordDetector(callback=on_wake)
    else:
        print("⚠️  No wake model found. Using Ctrl+Space to trigger OSIA.")
        stop_hotkey = _start_ctrl_space_listener(on_wake)

    # Serialises voice + text handling so two requests don't run in parallel.
    handler_lock = threading.Lock()

    # ── Agent run helpers ────────────────────────────────────────────────

    def _run_agent(user_text: str, *, voice_response: bool) -> None:
        """Run the agent in the current thread, persist + mirror the result.

        Caller must hold ``handler_lock`` and have already echoed the
        user's text into the popup. ``voice_response`` controls whether
        sentences are streamed to TTS as they arrive.
        """
        nonlocal active_conv

        # No active conversation (fresh start, or the last one was deleted)
        # — this first message creates it. Panel refresh goes through the
        # bridge: this runs on a worker thread, never touch Qt directly.
        if active_conv is None:
            active_conv = conversations.create_conversation()
            bridge.conversations_changed.emit(active_conv["id"])
            print(f"🆕 New conversation: {active_conv['id']}")

        prev_history_len = len(agent.messages) - 1  # skip system prompt
        bridge.island_state.emit("thinking")

        def on_first_token(*_a, **_kw):
            if voice_response:
                bridge.island_state.emit("speaking")

        try:
            on_sentence = speak_streaming if voice_response else None
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
            reply = agent.run(
                user_text,
                on_sentence=on_sentence,
                on_first_token=on_first_token,
            )
            if voice_response:
                print(
                    f"[TTS] main.wake_mode: agent.run() returned "
                    f"({len(reply or '')} chars) — sentences were streamed during the run",
                    flush=True,
                )
            _persist_agent_delta(
                active_conv["id"], agent, prev_history_len,
            )
            display = clean_response(reply or "")
            if display:
                bridge.chat_message.emit("assistant", display)
        except Exception as e:
            err = f"Error: {e}"
            bridge.chat_message.emit("assistant", err)
            if voice_response:
                print(f"[TTS] main.wake_mode: speaking error message: {err[:80]!r}", flush=True)
                threading.Thread(target=lambda: speak(err), daemon=True).start()
        finally:
            bridge.island_state.emit("idle")

    # ── Wake handler (voice path) ────────────────────────────────────────

    def handle_wake():
        if not handler_lock.acquire(blocking=False):
            print("⚠️  Wake fired while a handler is already running — ignored.")
            return
        tts_stop()

        # Bloc 4 — Live API voice-to-voice. When enabled, OSIA's local STT
        # (Whisper) transcription and TTS speak steps are bypassed entirely:
        # the provider's live/realtime endpoint streams audio in and speaks
        # back natively. The real WebSocket streaming is a separate task —
        # _connect_live_api() is currently a stub (see osia.voice.live_api).
        live_settings = load_settings()
        if live_settings.get("liveapi_enabled"):
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
                    handler_lock.release()

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
                bridge.chat_message.emit("user", user_text)
                _run_agent(user_text, voice_response=True)
            finally:
                handler_lock.release()

        threading.Thread(target=process, daemon=True).start()

    # ── Text handler (typed message in popup) ────────────────────────────

    def handle_send_text(text: str):
        if not handler_lock.acquire(blocking=False):
            print("⚠️  Send fired while a handler is running — ignored.")
            return
        tts_stop()
        bridge.chat_message.emit("user", text)

        def process():
            try:
                _run_agent(text, voice_response=False)
            finally:
                handler_lock.release()

        threading.Thread(target=process, daemon=True).start()

    # ── Popup chat / message wiring ─────────────────────────────────────

    def handle_chat_message(role: str, text: str):
        popup.append_message(role, text)

    def handle_island_state(state: str):
        island.set_state(state)

    # ── Conversation management ──────────────────────────────────────────

    def handle_new_conversation():
        nonlocal active_conv
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
        ok = conversations.delete_conversation(conv_id)
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

    # ── Model + settings wiring ─────────────────────────────────────────

    def handle_model_change(model: str):
        try:
            agent.set_model(model)
        except Exception as e:
            print(f"⚠️  Could not switch model: {e}")
            return
        set_setting("selected_model", model)
        popup.set_current_model(model)
        print(f"🔄 Model changed to: {model} (provider: {agent.provider})")

    def handle_keys_saved(_keys):
        discover_models_async(
            lambda remote: bridge.remote_models_signal.emit(remote)
        )

    def handle_remote_models(remote):
        popup.set_remote_models(remote)

    def handle_pick_file(path: str):
        # Surface the attachment as a user message so the agent can react.
        msg = f"[File attached: {path}]"
        handle_send_text(msg)

    # ── Signal wiring ───────────────────────────────────────────────────

    bridge.wake_detected.connect(handle_wake)
    bridge.chat_message.connect(handle_chat_message)
    bridge.island_state.connect(handle_island_state)
    bridge.remote_models_signal.connect(handle_remote_models)
    bridge.conversations_changed.connect(popup.refresh_conversations)
    bridge.new_chat.connect(handle_voice_new_chat)
    # WOR-22: Ctrl+Alt+O toggles the popup, keeping the GUI reachable even when
    # the floating capsule is hidden.
    bridge.toggle_popup.connect(popup.toggle)
    stop_popup_hotkey = _start_popup_hotkey_listener(bridge.toggle_popup.emit)

    popup.send_text.connect(handle_send_text)
    popup.pick_file.connect(handle_pick_file)
    popup.new_conversation.connect(handle_new_conversation)
    popup.open_conversation.connect(handle_open_conversation)
    popup.delete_conversation.connect(handle_delete_conversation)
    popup.model_changed.connect(handle_model_change)
    popup.api_keys_saved.connect(handle_keys_saved)
    # WOR-22: the "Show the floating capsule" toggle hides/shows the capsule.
    popup.capsule_visibility_changed.connect(island.set_capsule_visible)

    # The "Enable wake word" settings toggle stops/restarts the always-on
    # listener live. OSIA stays reachable via the capsule / Ctrl+Alt+O window
    # while it's off. No-op when no detector exists (no wake model available).
    def _set_wake_word_enabled(enabled: bool) -> None:
        if not detector:
            return
        if enabled:
            detector.start()
        else:
            detector.stop()
    popup.wake_word_enabled_changed.connect(_set_wake_word_enabled)

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

    def run_cron_task(task_text: str, conversation_id: str | None = None) -> None:
        if not handler_lock.acquire(timeout=120):
            print(f"[CRON] busy — scheduled task skipped: {task_text[:60]}")
            return
        try:
            tts_stop()
            sel = load_settings().get("selected_model")
            if sel:
                try:
                    cron_agent.set_model(sel)
                except Exception as e:
                    print(f"[CRON] set_model({sel!r}) failed: {e}")
            # Linked conversation (optional): load its history so context
            # carries across scheduled runs, and append this run's exchange so
            # the next run sees it. With none linked, each run is a fresh,
            # one-shot turn exactly as before.
            conv = (conversations.get_conversation(conversation_id)
                    if conversation_id else None)
            bridge.island_state.emit("thinking")
            if conv is not None:
                history = conv.get("messages") or []
                print(f"[CRON] conversation {conversation_id}: loaded "
                      f"{len(history)} messages for context")
                reply = cron_agent.run(task_text, history=history)
                _persist_exchange(conversation_id, task_text,
                                  clean_response(reply or ""))
            else:
                cron_agent.reset()  # each scheduled run is a fresh, one-shot turn
                reply = cron_agent.run(task_text)
            display = clean_response(reply or "")
            if display:
                bridge.chat_message.emit("assistant", f"⏰ {display}")
                speak(display)
        except Exception as e:
            print(f"[CRON] task error: {e}")
        finally:
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

    cron_manager = CronManager(run_cron_task)
    heartbeat_monitor = HeartbeatMonitor(heartbeat_alert)
    cron_manager.start()
    heartbeat_monitor.start()

    # Honour the saved "Enable wake word" preference at launch: when off, the
    # detector exists but isn't started (OSIA stays reachable via the capsule /
    # Ctrl+Alt+O window). The settings toggle can start it again at runtime.
    wake_word_on = settings.get("wake_word_enabled", True)
    if detector and wake_word_on:
        detector.start()
        trigger_label = "Say 'osia' to activate"
    elif detector:
        trigger_label = "Wake word off — click the capsule or press Ctrl+Alt+O"
    else:
        trigger_label = "Press Ctrl+Space to activate"

    print("=" * 60)
    print("  OSIA — Wake Mode (Dynamic Island)")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print(f"  {trigger_label}")
    print("  Ctrl+Alt+O: show/hide the OSIA window")
    print("=" * 60)

    def on_quit():
        # Stop the Bloc 5 background threads cleanly before the app tears down.
        cron_manager.stop()
        heartbeat_monitor.stop()
        if detector:
            detector.stop()
        if stop_hotkey:
            stop_hotkey()
        if stop_popup_hotkey:
            stop_popup_hotkey()

    app.aboutToQuit.connect(on_quit)

    perf_mark("wake_mode: entering app.exec() (GUI now interactive)")
    sys.exit(app.exec())


def main():
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
