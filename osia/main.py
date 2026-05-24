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
    """Wake word mode: continuous listening for wake word with wake screen GUI.
    Falls back to Ctrl+Space if no verifier model is available.
    This is the primary GUI mode — NO floating overlay, only wake screen.
    """
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QObject, pyqtSignal

    from osia.gui.wake_screen import (
        WakeScreen, ModelSelector, SettingsButton, SettingsPanel,
    )
    from osia.voice.wake import WakeWordDetector, verifier_available
    from osia.voice.stt import listen, listen_for_command
    from osia.voice.tts import speak, speak_streaming, stop as tts_stop
    from osia.agent import clean_response
    from osia.config import load_settings, get_setting
    from osia.models import select_model, discover_models_async

    class Bridge(QObject):
        wake_detected = pyqtSignal()
        response_ready_signal = pyqtSignal(str)
        status_signal = pyqtSignal(str)
        remote_models_signal = pyqtSignal(list)

    bridge = Bridge()

    app = QApplication(sys.argv)
    app.setApplicationName("OSIA")

    wake_screen = WakeScreen()
    model_selector = ModelSelector()
    settings_button = SettingsButton()
    settings_panel = SettingsPanel()

    # Fade companions in/out together with the wake screen.
    wake_screen.fade_in_started.connect(model_selector.fade_in)
    wake_screen.fade_in_started.connect(settings_button.fade_in)
    wake_screen.fade_out_started.connect(model_selector.fade_out)
    wake_screen.fade_out_started.connect(settings_button.fade_out)

    # Current agent instance
    agent = OSIAAgent()

    # Active conversation — every wake-word interaction is persisted to
    # this file under ~/.osia/conversations/. A fresh conversation is
    # created each time wake mode starts.
    active_conv = conversations.create_conversation()
    print(f"  Conversation: {active_conv['id']}")

    # Restore last-selected model if it's still available.
    settings = load_settings()
    initial_model = settings.get("selected_model")
    if initial_model:
        try:
            agent.set_model(initial_model)
            model_selector.set_current_model(initial_model)
        except Exception:
            pass

    smart_routing = bool(settings.get("smart_routing", False))

    # --- Wake word / hotkey trigger ---
    def on_wake():
        bridge.wake_detected.emit()

    model_available = verifier_available()
    detector = None
    stop_hotkey = None

    if model_available:
        detector = WakeWordDetector(callback=on_wake)
    else:
        print("⚠️  No wake model found. Using Ctrl+Space to trigger OSIA.")
        stop_hotkey = _start_ctrl_space_listener(on_wake)

    # Serialises wake handling so that a duplicate detection on the same
    # utterance (or an overlapping intentional trigger) can't spawn a second
    # STT+agent session on top of one already running. Held for the entire
    # lifetime of the process() worker, not just handle_wake itself.
    wake_handler_lock = threading.Lock()

    # --- Connect signals ---
    def handle_wake():
        if not wake_handler_lock.acquire(blocking=False):
            print("⚠️  Wake fired while a handler is already running — ignored.")
            return
        # Interrupt any in-flight TTS so the user can speak over OSIA.
        # The wake detector runs continuously (never paused during playback).
        tts_stop()
        wake_screen.set_state("listening")
        wake_screen.show_wake()

        def process():
            try:
                user_text = listen_for_command(max_retries=3)

                if not user_text:
                    wake_screen.hide_wake()
                    speak("Je n'ai pas compris, répétez.")
                    return

                print(f"📝 You said: {user_text}")
                wake_screen.set_state("thinking")
                wake_screen.hide_wake()

                # Smart routing: pick a model per-prompt if enabled.
                if smart_routing:
                    chosen = select_model(user_text, fallback_model=agent.model)
                    if chosen != agent.model:
                        try:
                            agent.set_model(chosen)
                            print(f"🧠 Smart-routed to: {chosen}")
                        except Exception as e:
                            print(f"⚠️  Smart routing failed: {e}")

                prev_history_len = len(agent.messages) - 1  # skip system prompt
                try:
                    # Stream sentences to TTS as the model produces them, so
                    # OSIA starts speaking within ~1s of the first chunk
                    # instead of waiting for the entire response.
                    agent.run(user_text, on_sentence=speak_streaming)
                    _persist_agent_delta(
                        active_conv["id"], agent, prev_history_len,
                    )
                except Exception as e:
                    bridge.response_ready_signal.emit(f"Error: {e}")
            finally:
                wake_handler_lock.release()

        threading.Thread(target=process, daemon=True).start()

    def handle_response(text):
        threading.Thread(target=lambda: speak(text), daemon=True).start()

    def handle_status(status):
        pass

    def handle_model_change(model):
        try:
            agent.set_model(model)
        except Exception as e:
            print(f"⚠️  Could not switch model: {e}")
            return
        from osia.config import set_setting
        set_setting("selected_model", model)
        print(f"🔄 Model changed to: {model} (provider: {agent.provider})")

    def handle_smart_toggled(enabled: bool):
        nonlocal smart_routing
        smart_routing = enabled
        print(f"🧠 Smart routing: {'ON' if enabled else 'OFF'}")

    def handle_keys_saved(_keys):
        # Re-discover remote models when keys change.
        discover_models_async(
            lambda remote: bridge.remote_models_signal.emit(remote)
        )

    def handle_settings_button_click():
        settings_panel.show()
        settings_panel.raise_()
        settings_panel.activateWindow()

    def handle_remote_models(remote):
        model_selector.refresh_models(remote_models=remote)

    bridge.wake_detected.connect(handle_wake)
    bridge.response_ready_signal.connect(handle_response)
    bridge.status_signal.connect(handle_status)
    bridge.remote_models_signal.connect(handle_remote_models)
    model_selector.model_changed.connect(handle_model_change)
    settings_button.clicked.connect(handle_settings_button_click)
    settings_panel.smart_routing_toggled.connect(handle_smart_toggled)
    settings_panel.api_keys_saved.connect(handle_keys_saved)

    # Kick off remote model discovery at startup.
    discover_models_async(
        lambda remote: bridge.remote_models_signal.emit(remote)
    )

    if detector:
        detector.start()
        trigger_label = "Say 'osia' to activate"
    else:
        trigger_label = "Press Ctrl+Space to activate"

    print("=" * 60)
    print("  OSIA — Wake Mode")
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
