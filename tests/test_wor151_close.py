"""WOR-151 shutdown-only UI and owned-service regressions."""

from __future__ import annotations

import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from osia import main
from osia.gui import main_popup
from osia.runtime import SERVER_MODE
from osia.utils import systemd_manager


_QT_APP = QApplication.instance() or QApplication([])


class _DormantThread:
    started = []

    def __init__(self, *args, target=None, name="", **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)
        self.name = name

    def start(self):
        self.started.append(self)


class _ImmediateThread(_DormantThread):
    def start(self):
        self.started.append(self)
        self.target()


@pytest.fixture
def panel(monkeypatch):
    _DormantThread.started.clear()
    monkeypatch.setattr(
        main_popup, "threading", types.SimpleNamespace(Thread=_DormantThread),
    )
    widget = main_popup._SettingsPanel()
    yield widget
    widget.close()
    widget.deleteLater()
    _QT_APP.processEvents()


def test_close_button_is_immediately_below_restart(panel):
    layout = panel._restart_btn.parentWidget().layout()
    assert layout.indexOf(panel._close_osia_btn) == layout.indexOf(panel._restart_btn) + 1
    assert panel._close_osia_btn.text() == "Close OSIA"


def test_cancel_performs_no_shutdown(monkeypatch, panel):
    stopped = []
    monkeypatch.setattr(panel, "_confirm_close_osia", lambda: False)
    monkeypatch.setattr(main_popup, "stop_service", lambda unit: stopped.append(unit))
    panel._on_close_osia_clicked()
    assert stopped == []
    assert not panel._shutdown_pending


def test_confirm_stops_only_owned_server_then_quits_successfully(
    monkeypatch, panel,
):
    calls = []
    fake_app = types.SimpleNamespace(
        setProperty=lambda key, value: calls.append((key, value)),
        quit=lambda: calls.append(("quit", 0)),
    )
    monkeypatch.setattr(panel, "_confirm_close_osia", lambda: True)
    monkeypatch.setattr(
        main_popup, "threading", types.SimpleNamespace(Thread=_ImmediateThread),
    )
    monkeypatch.setattr(
        main_popup, "stop_service", lambda unit: calls.append(("stop", unit)) or True,
    )
    monkeypatch.setattr(
        main_popup, "QApplication", types.SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        main_popup, "QTimer",
        types.SimpleNamespace(singleShot=lambda _delay, callback: callback()),
    )

    panel._on_close_osia_clicked()

    assert calls == [
        ("stop", "osia-server.service"),
        ("osiaIntentionalShutdown", True),
        ("quit", 0),
    ]
    assert panel._shutdown_pending
    assert not panel._restart_pending


def test_repeated_close_is_idempotent_and_disables_restart(monkeypatch, panel):
    confirmations = []
    monkeypatch.setattr(
        panel, "_confirm_close_osia", lambda: confirmations.append(True) or True,
    )
    panel._on_close_osia_clicked()
    panel._on_close_osia_clicked()
    assert confirmations == [True]
    assert len([thread for thread in _DormantThread.started if thread.name == "osia-session-shutdown"]) == 1
    assert not panel._restart_btn.isEnabled()
    assert not panel._close_osia_btn.isEnabled()


def test_shutdown_failure_is_retryable(monkeypatch, panel):
    monkeypatch.setattr(panel, "_confirm_close_osia", lambda: True)
    panel._on_close_osia_clicked()
    panel._on_shutdown_finished(False)
    assert not panel._shutdown_pending
    assert panel._restart_btn.isEnabled()
    assert panel._close_osia_btn.isEnabled()
    assert panel._power_status_key == "settings.close_error"


def test_desktop_quit_hides_windows_before_blocking_cleanup():
    calls = []
    quit_callbacks = []
    visible = types.SimpleNamespace(
        isVisible=lambda: True,
        hide=lambda: calls.append("hide"),
    )
    hidden = types.SimpleNamespace(
        isVisible=lambda: False,
        hide=lambda: pytest.fail("Already hidden window was hidden again"),
    )

    def run_loop():
        for callback in quit_callbacks:
            callback()
        assert calls == ["hide"]
        calls.append("loop exited")
        return 7

    app = types.SimpleNamespace(
        aboutToQuit=types.SimpleNamespace(connect=quit_callbacks.append),
        topLevelWidgets=lambda: [visible, hidden],
        exec=run_loop,
    )
    exit_code = main._exec_desktop_with_cleanup(app, lambda: calls.append("cleanup"))
    assert exit_code == 7
    assert calls == ["hide", "loop exited", "cleanup"]


def test_windows_stop_preserves_scheduled_task_and_never_relaunches(monkeypatch):
    calls = []
    monkeypatch.setattr(systemd_manager, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        systemd_manager, "_win_coordinated_stop",
        lambda kind: calls.append(("stop", kind)) or True,
    )
    monkeypatch.setattr(
        systemd_manager, "_win_delete_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("task deleted")),
    )
    monkeypatch.setattr(
        systemd_manager, "_win_run_task",
        lambda *_args: (_ for _ in ()).throw(AssertionError("service relaunched")),
    )
    assert systemd_manager.stop_service("osia-server.service")
    assert calls == [("stop", "server")]


@pytest.mark.parametrize(
    ("frozen", "cmdline"),
    [
        (False, ["pythonw.exe", r"C:\Users\test\osia\osia\server\run_server.py"]),
        (True, [r"C:\Program Files\OSIA\OSIA.exe", SERVER_MODE]),
    ],
)
def test_source_and_frozen_server_ownership_is_exact(monkeypatch, frozen, cmdline):
    monkeypatch.setattr(systemd_manager, "is_frozen", lambda: frozen)
    info = {"cmdline": cmdline}
    if not frozen:
        monkeypatch.setattr(
            systemd_manager,
            "_REPO_ROOT",
            systemd_manager.Path(r"C:\Users\test\osia"),
        )
    assert systemd_manager._win_is_service_owner(info, "server")
    assert not systemd_manager._win_is_service_owner(
        {"cmdline": ["python.exe", "unrelated.py"]}, "server",
    )
