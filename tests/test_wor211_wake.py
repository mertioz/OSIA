from __future__ import annotations

import importlib.util
import inspect
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import config, i18n
from osia.voice import wake


ROOT = Path(__file__).resolve().parents[1]


def test_acoustic_release_contract_is_false_in_source_and_packaged_modes(
    monkeypatch, tmp_path,
):
    retired_model = tmp_path / "osia_verifier.pkl"
    retired_model.write_bytes(b"retired")
    monkeypatch.setattr(wake, "VERIFIER_PATH", str(retired_model), raising=False)

    for frozen in (False, True):
        monkeypatch.setattr(sys, "frozen", frozen, raising=False)
        assert wake.verifier_available() is False
        assert wake.acoustic_wake_status() == {
            "acoustic_wake_available": False,
            "reason": "release_disabled_no_reliable_local_verifier",
        }


def test_legacy_persisted_true_and_future_setting_write_stay_disabled(monkeypatch):
    monkeypatch.setattr(
        config,
        "_read_settings_file",
        lambda: {"wake_word_enabled": True},
    )
    assert config.load_settings()["wake_word_enabled"] is False

    saved = []
    monkeypatch.setattr(config, "load_settings", lambda: {"wake_word_enabled": True})
    monkeypatch.setattr(config, "save_settings", lambda settings: saved.append(settings))
    config.set_setting("wake_word_enabled", True)
    assert saved == [{"wake_word_enabled": False}]


def test_retired_detector_start_loads_no_model_stream_or_listener(monkeypatch):
    calls = []

    def forbidden(name):
        def factory(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must not be called")

        return factory

    monkeypatch.setattr(
        wake,
        "sd",
        SimpleNamespace(InputStream=forbidden("input_stream")),
    )
    before = [
        thread for thread in threading.enumerate()
        if thread.name == "osia-wake-listener"
    ]
    detector = wake.WakeWordDetector(
        callback=forbidden("callback"),
        _feature_extractor_factory=forbidden("openwakeword"),
        _verifier_factory=forbidden("verifier"),
        _vad_factory=forbidden("vad"),
        _stream_factory=forbidden("input_stream"),
    )

    assert detector.start() is False
    assert detector._thread is None
    assert detector.is_generation_current(detector.generation) is False
    assert detector.request_rearm(detector.generation) is False
    detector.stop()

    after = [
        thread for thread in threading.enumerate()
        if thread.name == "osia-wake-listener"
    ]
    assert calls == []
    assert before == after == []


def test_manual_gate_is_one_shot_and_rejects_duplicate_busy_and_stale_events():
    handler_lock = threading.Lock()
    state = {"agent": False, "tts": False}
    gate = wake.WakeAcceptanceGate(
        handler_lock,
        is_busy=lambda: state["agent"],
        is_tts_active=lambda: state["tts"],
    )

    assert gate.reserve(0) is True
    assert gate.reserve(0) is False
    assert gate.consume(0, is_current=True) is True
    assert handler_lock.locked()
    gate.finish()

    handler_lock.acquire()
    assert gate.reserve(0) is False
    handler_lock.release()
    state["agent"] = True
    assert gate.reserve(0) is False
    state["agent"] = False
    state["tts"] = True
    assert gate.reserve(0) is False
    state["tts"] = False

    assert gate.reserve(0) is True
    assert gate.consume(0, is_current=False) is False
    assert not handler_lock.locked()
    assert gate.consume(0, is_current=True) is False


def test_manual_gate_shutdown_cancels_a_queued_event():
    handler_lock = threading.Lock()
    gate = wake.WakeAcceptanceGate(handler_lock)
    assert gate.reserve(0) is True
    gate.cancel(0)
    assert not handler_lock.locked()
    assert gate.consume(0, is_current=True) is False


def test_ctrl_alt_p_listener_has_one_owner_and_gate_deduplicates(monkeypatch):
    listeners = []

    class GlobalHotKeys:
        def __init__(self, mapping):
            self.mapping = mapping
            self.daemon = False
            self.started = 0
            self.stopped = 0
            listeners.append(self)

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1

    keyboard = SimpleNamespace(GlobalHotKeys=GlobalHotKeys)
    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace(keyboard=keyboard))

    from osia import main

    gate = wake.WakeAcceptanceGate(threading.Lock())
    events = []

    def activate():
        if gate.reserve(0):
            events.append(0)

    stop = main._start_ctrl_alt_p_listener(activate)
    assert len(listeners) == 1
    listener = listeners[0]
    assert listener.started == 1
    assert set(listener.mapping) == {"<ctrl>+<alt>+p"}
    assert "<ctrl>+<space>" not in listener.mapping
    listener.mapping["<ctrl>+<alt>+p"]()
    listener.mapping["<ctrl>+<alt>+p"]()
    assert events == [0]
    assert gate.consume(0, is_current=True) is True
    gate.finish()
    listener.mapping["<ctrl>+<alt>+p"]()
    assert events == [0, 0]
    assert gate.consume(0, is_current=True) is True
    gate.finish()
    stop()
    assert listener.stopped == 1


def test_ctrl_alt_p_listener_reports_callback_failure_without_sensitive_data(
    monkeypatch, capsys,
):
    listeners = []

    class GlobalHotKeys:
        def __init__(self, mapping):
            self.mapping = mapping
            listeners.append(self)

        def start(self):
            pass

        def stop(self):
            pass

    keyboard = SimpleNamespace(GlobalHotKeys=GlobalHotKeys)
    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace(keyboard=keyboard))

    from osia import main

    def fail():
        raise RuntimeError("private callback detail")

    main._start_ctrl_alt_p_listener(fail)
    listeners[0].mapping["<ctrl>+<alt>+p"]()
    output = capsys.readouterr().out
    assert "[wake] Ctrl+Alt+P callback failed: RuntimeError" in output
    assert "private callback detail" not in output


def test_popup_hotkey_remains_ctrl_alt_o_and_cleans_up(monkeypatch):
    listeners = []

    class GlobalHotKeys:
        def __init__(self, mapping):
            self.mapping = mapping
            self.daemon = False
            self.stopped = 0
            listeners.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped += 1

    keyboard = SimpleNamespace(GlobalHotKeys=GlobalHotKeys)
    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace(keyboard=keyboard))

    from osia import main

    stop = main._start_popup_hotkey_listener(lambda: None)
    assert len(listeners) == 1
    assert set(listeners[0].mapping) == {"<ctrl>+<alt>+o"}
    assert stop is not None
    stop()
    assert listeners[0].stopped == 1


def test_main_uses_only_intentional_manual_activation_and_preserves_access_paths():
    from osia import main

    source = inspect.getsource(main.wake_mode)
    assert "WakeWordDetector" not in source
    assert "verifier_available" not in source
    assert "No wake model found" not in source
    assert "ACOUSTIC_WAKE_UNAVAILABLE_REASON" in source
    assert "_start_ctrl_alt_p_listener(_manual_wake)" in source
    assert "if not manual_activation_open.is_set():" in source
    assert "if not wake_gate.reserve(0):" in source
    assert '"[wake] manual activation accepted"' in source
    assert "bridge.wake_detected.emit(0)" in source
    assert "wake_gate.cancel(0)" in source
    assert "island.clicked.connect(popup.toggle)" in source
    assert "_start_popup_hotkey_listener(bridge.toggle_popup.emit)" in source
    assert "wake_word_enabled_changed.connect" not in source


def test_settings_surface_is_disabled_and_truthful():
    popup_source = (ROOT / "osia" / "gui" / "main_popup.py").read_text("utf-8")
    assert "self._wake_word_toggle.setEnabled(False)" in popup_source
    assert "set_setting(\"wake_word_enabled\", False)" in popup_source
    assert 'self._wake_word_toggle.setChecked(False)' in popup_source
    assert i18n.TRANSLATIONS["settings.wake_word_enabled"]["en"] == (
        "Press Ctrl+Alt+P to talk to OSIA."
    )
    assert i18n.TRANSLATIONS["settings.wake_word_enabled"]["fr"] == (
        "Pour parler à OSIA, appuyez sur Ctrl+Alt+P."
    )


def test_source_and_packaged_desktop_share_the_same_manual_pipeline(monkeypatch):
    from osia import relaunch

    token = object()
    calls = []
    monkeypatch.setattr(relaunch.sys, "platform", "win32")
    monkeypatch.setattr(relaunch, "_desktop_mutex_owned", None)
    monkeypatch.setattr(
        relaunch,
        "_acquire_windows_mutex",
        lambda *args, **kwargs: calls.append((args, kwargs)) or token,
    )
    assert relaunch.acquire_desktop_instance_mutex() is token
    assert relaunch.acquire_desktop_instance_mutex() is token
    assert len(calls) == 1

    spec = importlib.util.spec_from_file_location(
        "wor211_packaging_launcher",
        ROOT / "packaging" / "windows" / "launcher.py",
    )
    launcher = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(launcher)
    assert launcher._acquire_gui_mutex() is token
    assert len(calls) == 1

    launcher_source = inspect.getsource(launcher.main)
    assert "from osia.main import main as desktop_main" in launcher_source
    assert "osia.voice.wake" not in launcher_source


def test_retired_verifier_is_absent_from_runtime_and_package_manifest():
    wake_source = (ROOT / "osia" / "voice" / "wake.py").read_text("utf-8")
    package_spec = (
        ROOT / "packaging" / "windows" / "osia.spec"
    ).read_text("utf-8")
    assert "osia_verifier.pkl" not in wake_source
    assert "osia_verifier.pkl" not in package_spec
    assert not (ROOT / "osia" / "wake_training" / "osia_verifier.pkl").exists()
    assert '(str(STAGE / "models"), "models")' in package_spec
    assert "collect_data_files(\"openwakeword\")" in package_spec
