"""Focused WOR-148 regressions for windowless probes and instant navigation."""

from __future__ import annotations

import subprocess
import sys
import threading
import types
from datetime import datetime

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from osia import models
from osia.gui import main_popup
from osia.utils import subprocess_policy as policy


class _FakeStartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 99
        self.marker = "kept"


def _windows_constants(monkeypatch):
    values = {
        "CREATE_NO_WINDOW": 0x08000000,
        "CREATE_NEW_PROCESS_GROUP": 0x00000200,
        "CREATE_NEW_CONSOLE": 0x00000010,
        "DETACHED_PROCESS": 0x00000008,
        "STARTF_USESHOWWINDOW": 0x00000001,
        "SW_HIDE": 0,
        "STARTUPINFO": _FakeStartupInfo,
    }
    for name, value in values.items():
        monkeypatch.setattr(policy.subprocess, name, value, raising=False)
    return values


@pytest.mark.parametrize(
    "mode",
    [policy.ProcessMode.BACKGROUND_PROBE, policy.ProcessMode.BACKGROUND_SUPERVISED],
)
def test_windows_background_process_merges_flags_and_preserves_startupinfo(
    monkeypatch, mode,
):
    constants = _windows_constants(monkeypatch)
    original = _FakeStartupInfo()
    existing_flag = 0x00000400

    kwargs = policy.process_kwargs(
        mode,
        {"creationflags": existing_flag, "startupinfo": original},
        platform="win32",
    )

    assert kwargs["creationflags"] == existing_flag | constants["CREATE_NO_WINDOW"]
    assert kwargs["startupinfo"] is not original
    assert kwargs["startupinfo"].marker == "kept"
    assert kwargs["startupinfo"].dwFlags & constants["STARTF_USESHOWWINDOW"]
    assert kwargs["startupinfo"].wShowWindow == constants["SW_HIDE"]
    assert original.dwFlags == 0


@pytest.mark.parametrize(
    "mode",
    [
        policy.ProcessMode.INTERACTIVE,
        policy.ProcessMode.BROWSER_OPEN,
        policy.ProcessMode.INSTALLER_UAC,
    ],
)
def test_interactive_windows_processes_are_not_hidden(monkeypatch, mode):
    _windows_constants(monkeypatch)
    assert policy.process_kwargs(mode, {"text": True}, platform="win32") == {
        "text": True
    }


@pytest.mark.parametrize("platform_name", ["linux", "darwin"])
def test_non_windows_processes_receive_no_windows_options(platform_name):
    kwargs = policy.process_kwargs(
        policy.ProcessMode.BACKGROUND_PROBE,
        {"creationflags": 123, "startupinfo": object(), "text": True},
        platform=platform_name,
    )
    assert kwargs == {"text": True}


def test_relaunch_flags_do_not_mix_detached_and_create_no_window(monkeypatch):
    constants = _windows_constants(monkeypatch)
    kwargs = policy.process_kwargs(
        policy.ProcessMode.RELAUNCH_COORDINATOR,
        {"creationflags": constants["CREATE_NO_WINDOW"]},
        platform="win32",
    )
    flags = kwargs["creationflags"]
    assert flags & constants["DETACHED_PROCESS"]
    assert flags & constants["CREATE_NEW_PROCESS_GROUP"]
    assert not flags & constants["CREATE_NO_WINDOW"]


def test_hidden_process_rejects_new_console_and_shell_true(monkeypatch):
    constants = _windows_constants(monkeypatch)
    with pytest.raises(ValueError, match="CREATE_NEW_CONSOLE"):
        policy.process_kwargs(
            policy.ProcessMode.BACKGROUND_PROBE,
            {"creationflags": constants["CREATE_NEW_CONSOLE"]},
            platform="win32",
        )
    with pytest.raises(ValueError, match="shell=False"):
        policy.process_kwargs(
            policy.ProcessMode.BACKGROUND_PROBE,
            {"shell": True},
            platform="win32",
        )


def test_background_probe_keeps_stdout_stderr_and_exit_code():
    result = policy.run_background_probe(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
        ],
        text=True,
        check=False,
    )
    assert result.returncode == 7
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


@pytest.fixture
def clean_ollama_cache(monkeypatch):
    monkeypatch.setattr(models, "_OLLAMA_CACHE", None)
    monkeypatch.setattr(models, "_OLLAMA_CACHE_AT", 0.0)
    monkeypatch.setattr(models, "_OLLAMA_CACHE_EPOCH", 0)
    monkeypatch.setattr(models, "_OLLAMA_REFRESH_IN_FLIGHT", False)
    monkeypatch.setattr(models, "_OLLAMA_REFRESH_PENDING", False)
    monkeypatch.setattr(models, "_OLLAMA_REFRESH_CALLBACKS", [])


def test_cached_registry_never_launches_a_probe(monkeypatch, clean_ollama_cache):
    monkeypatch.setattr(
        models,
        "_query_ollama_models",
        lambda: (_ for _ in ()).throw(AssertionError("synchronous probe")),
    )
    assert models.get_all_models_cached()


def test_rapid_refresh_requests_share_one_probe(monkeypatch, clean_ollama_cache):
    threads = []
    delivered = []

    class ControlledThread:
        def __init__(self, *args, target=None, **_kwargs):
            self.target = target if target is not None else args[0]
            threads.append(self)

        def start(self):
            return

    monkeypatch.setattr(models.threading, "Thread", ControlledThread)
    monkeypatch.setattr(
        models,
        "_query_ollama_models",
        lambda: [{"id": "local", "provider": "ollama"}],
    )

    models.refresh_ollama_models_async(delivered.append)
    models.refresh_ollama_models_async(delivered.append)

    assert len(threads) == 1
    threads[0].target()
    assert models.cached_ollama_models() == [{"id": "local", "provider": "ollama"}]
    assert delivered == [[{"id": "local", "provider": "ollama"}]]


def test_stale_ollama_result_cannot_overwrite_newer_refresh(
    monkeypatch, clean_ollama_cache,
):
    threads = []
    results = iter(
        [
            [{"id": "stale", "provider": "ollama"}],
            [{"id": "fresh", "provider": "ollama"}],
        ]
    )

    class ControlledThread:
        def __init__(self, *args, target=None, **_kwargs):
            self.target = target if target is not None else args[0]
            threads.append(self)

        def start(self):
            return

    monkeypatch.setattr(models.threading, "Thread", ControlledThread)
    monkeypatch.setattr(models, "_query_ollama_models", lambda: next(results))

    models.refresh_ollama_models_async()
    models.invalidate_ollama_models_cache()
    threads[0].target()
    assert models.cached_ollama_models() == []
    assert len(threads) == 2
    threads[1].target()
    assert models.cached_ollama_models() == [{"id": "fresh", "provider": "ollama"}]


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


class _DormantThread:
    started = []

    def __init__(self, *args, target=None, name="", **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)
        self.name = name

    def start(self):
        self.started.append(self)


def test_page_navigation_starts_no_synchronous_process(
    monkeypatch, qt_app,
):
    _DormantThread.started.clear()
    refresh_requests = []
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    monkeypatch.setattr(
        main_popup,
        "refresh_ollama_models_async",
        lambda callback: refresh_requests.append(callback),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("navigation launched subprocess.run")
        ),
    )

    popup = main_popup.MainPopup()
    popup.resize(640, 560)
    popup.show()
    qt_app.processEvents()
    settings_loads = []
    model_rebuilds = []
    popup._settings._load_state = lambda: settings_loads.append(True)
    popup._models.refresh = lambda: model_rebuilds.append(True)

    popup._on_nav_clicked(popup.PAGE_MODELS)
    popup._on_nav_clicked(popup.PAGE_SETTINGS)

    assert settings_loads == []
    assert model_rebuilds == []
    assert refresh_requests

    observed_threads = []
    popup._ollama_models_ready.connect(
        lambda _items: observed_threads.append(QThread.currentThread())
    )
    worker = threading.Thread(
        target=lambda: refresh_requests[0]([
            {"id": "local", "provider": "ollama"}
        ])
    )
    worker.start()
    worker.join()
    qt_app.processEvents()
    assert observed_threads == [qt_app.thread()]
    popup.close()
    popup.deleteLater()
    qt_app.processEvents()


def test_chatgpt_status_cache_and_single_flight(monkeypatch, qt_app):
    _DormantThread.started.clear()
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    panel = main_popup._SettingsPanel()
    panel._chatgpt_probe_busy = False
    panel._chatgpt_action_busy = False
    panel._chatgpt_last_status = None
    _DormantThread.started.clear()

    panel._probe_chatgpt_account()
    first_generation = panel._chatgpt_request_generation
    panel._probe_chatgpt_account()
    assert len(_DormantThread.started) == 1

    panel._probe_chatgpt_account(force=True)
    assert len(_DormantThread.started) == 1
    assert panel._chatgpt_request_generation > first_generation
    old = {"connected": True}
    panel._chatgpt_last_status = old
    panel._apply_chatgpt_status({
        "connected": False,
        "_osiaRequestGeneration": first_generation,
        "_osiaRequestKind": "probe",
    })
    assert panel._chatgpt_last_status is old

    panel._chatgpt_probe_busy = False
    panel._chatgpt_refresh_pending = False
    panel._chatgpt_last_status = {"connected": False}
    panel._chatgpt_last_probe = datetime.now()
    _DormantThread.started.clear()
    panel._probe_chatgpt_account()
    assert _DormantThread.started == []
    panel.close()
    panel.deleteLater()
    qt_app.processEvents()


def test_startup_status_cache_and_single_flight(monkeypatch, qt_app):
    _DormantThread.started.clear()
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    panel = main_popup._SettingsPanel()
    panel._startup_probe_busy = False
    panel._startup_last_probe = datetime.now()
    _DormantThread.started.clear()

    panel._refresh_startup_status()
    assert _DormantThread.started == []

    panel._refresh_startup_status(force=True)
    panel._refresh_startup_status(force=True)
    assert len(_DormantThread.started) == 1
    assert panel._startup_refresh_pending is True
    panel.close()
    panel.deleteLater()
    qt_app.processEvents()


def test_chatgpt_retry_invalidates_cache_and_starts_real_login_path(
    monkeypatch, qt_app,
):
    _DormantThread.started.clear()
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    panel = main_popup._SettingsPanel()
    panel._chatgpt_probe_busy = False
    panel._chatgpt_action_busy = False
    panel._chatgpt_last_probe = datetime.now()
    generation = panel._chatgpt_request_generation
    _DormantThread.started.clear()

    panel._on_chatgpt_retry()

    assert panel._chatgpt_last_probe == datetime.min
    assert panel._chatgpt_request_generation == generation + 1
    assert [thread.name for thread in _DormantThread.started] == [
        "osia-chatgpt-login"
    ]
    panel.close()
    panel.deleteLater()
    qt_app.processEvents()
