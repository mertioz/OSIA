#!/usr/bin/env python3
import os
os.environ["QT_QPA_PLATFORM"] = "xcb"
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


# ── Modes ───────────────────────────────────────────────────────────────────

def text_mode():
    """Interactive text-based chat with OSIA."""
    agent = OSIAAgent()
    active_conv = conversations.create_conversation()
    print("=" * 60)
    print("  OSIA — Local AI Voice Assistant (text mode)")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print("  Type 'exit' or 'quit' to stop, 'reset' to clear history")
    print(f"  Conversation: {active_conv['id']}")
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
            active_conv = conversations.create_conversation()
            print(f"[New conversation: {active_conv['id']}]")
            continue

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

    active_conv = conversations.create_conversation()
    print("=" * 60)
    print("  OSIA — Voice Mode")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print(f"  Conversation: {active_conv['id']}")
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

        print("🤔 Thinking...")
        response = run_agent(user_text)
        response = clean_response(response)
        print(f"💬 OSIA: {response}")

        _persist_exchange(active_conv["id"], user_text, response)
        speak(response)


def wake_mode():
    """Wake word mode: continuous listening for wake word, driven by the
    Dynamic Island GUI. Falls back to Ctrl+Space if no verifier model is
    available.
    """
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QObject, pyqtSignal

    from osia.gui.dynamic_island import DynamicIsland
    from osia.gui.main_popup import MainPopup
    from osia.voice.wake import WakeWordDetector, verifier_available
    from osia.voice.stt import listen_for_command
    from osia.voice.tts import speak, speak_streaming, stop as tts_stop
    from osia.agent import clean_response
    from osia.config import load_settings, set_setting
    from osia.models import select_model, discover_models_async

    class Bridge(QObject):
        wake_detected = pyqtSignal()
        remote_models_signal = pyqtSignal(list)
        # Update the chat with a (role, text) pair from a worker thread.
        chat_message = pyqtSignal(str, str)
        # Drive the island's visual state from a worker thread.
        island_state = pyqtSignal(str)

    bridge = Bridge()

    app = QApplication(sys.argv)
    app.setApplicationName("OSIA")

    island = DynamicIsland()
    popup = MainPopup()

    # Toggle the popup when the island is clicked.
    island.clicked.connect(popup.toggle)

    # Agent + active conversation state
    agent = OSIAAgent()
    active_conv = conversations.create_conversation()
    print(f"  Conversation: {active_conv['id']}")

    settings = load_settings()
    initial_model = settings.get("selected_model")
    if initial_model:
        try:
            agent.set_model(initial_model)
            print(f"[DEBUG] after set_model({initial_model!r}): provider = {agent.provider}")
        except Exception as e:
            print(f"[DEBUG] set_model({initial_model!r}) failed: {e}")
    popup.set_current_model(agent.model)

    smart_routing = bool(settings.get("smart_routing", False))

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
        if smart_routing:
            chosen = select_model(user_text, fallback_model=agent.model)
            if chosen != agent.model:
                try:
                    agent.set_model(chosen)
                    print(f"🧠 Smart-routed to: {chosen}")
                except Exception as e:
                    print(f"⚠️  Smart routing failed: {e}")

        prev_history_len = len(agent.messages) - 1  # skip system prompt
        bridge.island_state.emit("thinking")

        def on_first_token(*_a, **_kw):
            if voice_response:
                bridge.island_state.emit("speaking")

        try:
            on_sentence = speak_streaming if voice_response else None
            reply = agent.run(
                user_text,
                on_sentence=on_sentence,
                on_first_token=on_first_token,
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
                threading.Thread(target=lambda: speak(err), daemon=True).start()
        finally:
            bridge.island_state.emit("idle")

    # ── Wake handler (voice path) ────────────────────────────────────────

    def handle_wake():
        if not handler_lock.acquire(blocking=False):
            print("⚠️  Wake fired while a handler is already running — ignored.")
            return
        tts_stop()
        bridge.island_state.emit("listening")

        def process():
            try:
                user_text = listen_for_command(max_retries=3)
                if not user_text:
                    bridge.island_state.emit("idle")
                    speak("Je n'ai pas compris, répétez.")
                    return
                print(f"📝 You said: {user_text}")
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
            active_conv = conversations.create_conversation()
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

    def handle_smart_toggled(enabled: bool):
        nonlocal smart_routing
        smart_routing = enabled
        print(f"🧠 Smart routing: {'ON' if enabled else 'OFF'}")

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

    popup.send_text.connect(handle_send_text)
    popup.pick_file.connect(handle_pick_file)
    popup.new_conversation.connect(handle_new_conversation)
    popup.open_conversation.connect(handle_open_conversation)
    popup.delete_conversation.connect(handle_delete_conversation)
    popup.model_changed.connect(handle_model_change)
    popup.smart_routing_toggled.connect(handle_smart_toggled)
    popup.api_keys_saved.connect(handle_keys_saved)

    # Initial population
    popup.refresh_conversations(active_conv["id"])
    discover_models_async(
        lambda remote: bridge.remote_models_signal.emit(remote)
    )

    if detector:
        detector.start()
        trigger_label = "Say 'osia' to activate"
    else:
        trigger_label = "Press Ctrl+Space to activate"

    print("=" * 60)
    print("  OSIA — Wake Mode (Dynamic Island)")
    print(f"  Language: {DEFAULT_LANG.upper()}")
    print(f"  Smart routing: {'ON' if smart_routing else 'OFF'}")
    print(f"  {trigger_label}")
    print("=" * 60)

    def on_quit():
        if detector:
            detector.stop()
        if stop_hotkey:
            stop_hotkey()

    app.aboutToQuit.connect(on_quit)

    sys.exit(app.exec())


def main():
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
