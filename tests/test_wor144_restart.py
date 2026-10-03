"""Focused regressions for the controlled OSIA desktop relaunch."""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from osia import relaunch
from osia.gui import main_popup


ROOT = Path(__file__).resolve().parent.parent
_QT_APP = QApplication.instance() or QApplication([])


def _load_launcher():
    path = ROOT / "packaging" / "windows" / "launcher.py"
    spec = importlib.util.spec_from_file_location("wor144_packaging_launcher", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def qt_app():
    yield _QT_APP


class _DormantThread:
    def __init__(self, *args, target=None, **_kwargs):
        self.target = target if target is not None else (args[0] if args else None)

    def start(self) -> None:
        return


@pytest.fixture
def settings_panel(monkeypatch, qt_app):
    monkeypatch.setattr(
        main_popup,
        "threading",
        types.SimpleNamespace(Thread=_DormantThread),
    )
    panel = main_popup._SettingsPanel()
    panel.resize(640, 560)
    panel.show()
    qt_app.processEvents()
    yield panel
    panel.close()
    panel.deleteLater()
    qt_app.processEvents()


def test_restart_confirmation_cancel_does_nothing(
    monkeypatch, settings_panel,
):
    prepared = []
    monkeypatch.setattr(
        main_popup.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel,
    )
    monkeypatch.setattr(
        relaunch,
        "prepare_relaunch",
        lambda: prepared.append(True),
    )

    settings_panel._on_restart_clicked()

    assert prepared == []
    assert settings_panel._restart_pending is False
    assert settings_panel._restart_btn.isEnabled()


def test_restart_confirmation_prepares_once_and_disables_duplicates(
    monkeypatch, settings_panel,
):
    prepared = []
    questions = []
    quits = []
    fake_app = types.SimpleNamespace(quit=lambda: quits.append(True))
    monkeypatch.setattr(
        main_popup.QMessageBox,
        "question",
        lambda *_args, **_kwargs: (
            questions.append(True) or QMessageBox.StandardButton.Yes
        ),
    )
    monkeypatch.setattr(
        relaunch,
        "prepare_relaunch",
        lambda: prepared.append(True),
    )
    monkeypatch.setattr(
        main_popup,
        "QApplication",
        types.SimpleNamespace(instance=lambda: fake_app),
    )
    monkeypatch.setattr(
        main_popup,
        "QTimer",
        types.SimpleNamespace(singleShot=lambda _delay, callback: callback()),
    )

    settings_panel._on_restart_clicked()
    settings_panel._on_restart_clicked()

    assert questions == [True]
    assert prepared == [True]
    assert quits == [True]
    assert settings_panel._restart_pending is True
    assert not settings_panel._restart_btn.isEnabled()
    assert settings_panel._restart_status.isVisible()


def test_restart_preparation_failure_is_visible_and_retryable(
    monkeypatch, settings_panel,
):
    monkeypatch.setattr(
        main_popup.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        relaunch,
        "prepare_relaunch",
        lambda: (_ for _ in ()).throw(OSError("private local detail")),
    )

    settings_panel._on_restart_clicked()

    assert settings_panel._restart_pending is False
    assert settings_panel._restart_btn.isEnabled()
    assert settings_panel._restart_status.isVisible()
    assert "private local detail" not in settings_panel._restart_status.text()


@pytest.mark.parametrize(
    ("frozen", "expected_coordinator", "expected_desktop"),
    [
        (
            False,
            ["C:\\Python\\python.exe", "-m", "osia.relaunch", "--coordinator", "41"],
            ["C:\\Python\\python.exe", "-m", "osia.main", "--wake"],
        ),
        (
            True,
            ["C:\\OSIA\\OSIA.exe", "--osia-relaunch", "41"],
            ["C:\\OSIA\\OSIA.exe", "--wake"],
        ),
    ],
)
def test_source_and_frozen_command_construction(
    monkeypatch, frozen, expected_coordinator, expected_desktop,
):
    executable = Path(
        "C:/OSIA/OSIA.exe" if frozen else "C:/Python/python.exe"
    )
    monkeypatch.setattr(relaunch, "application_executable", lambda: executable)

    assert relaunch.coordinator_command(41, frozen=frozen) == expected_coordinator
    assert relaunch.desktop_command(frozen=frozen) == expected_desktop


def test_prepare_relaunch_rejects_duplicate_requests(
    monkeypatch, tmp_path,
):
    started = []
    marker = tmp_path / "restart_pending.json"
    monkeypatch.setattr(relaunch, "RELAUNCH_MARKER", marker)
    monkeypatch.setattr(relaunch, "_request_pending", False)
    monkeypatch.setattr(relaunch, "coordinator_command", lambda pid: ["coord", str(pid)])
    monkeypatch.setattr(relaunch, "_detached_kwargs", lambda: {"detached": True})

    relaunch.prepare_relaunch(
        parent_pid=73,
        process_factory=lambda command, **kwargs: started.append((command, kwargs)),
    )

    assert started == [(["coord", "73"], {"detached": True})]
    assert marker.is_file()
    with pytest.raises(RuntimeError, match="restart_already_pending"):
        relaunch.prepare_relaunch(parent_pid=73, process_factory=lambda *_a, **_k: None)


def test_prepare_relaunch_failure_clears_marker_and_pending(
    monkeypatch, tmp_path,
):
    marker = tmp_path / "restart_pending.json"
    monkeypatch.setattr(relaunch, "RELAUNCH_MARKER", marker)
    monkeypatch.setattr(relaunch, "_request_pending", False)
    monkeypatch.setattr(relaunch, "coordinator_command", lambda pid: ["coord", str(pid)])
    monkeypatch.setattr(relaunch, "_detached_kwargs", lambda: {})

    with pytest.raises(OSError, match="spawn failed"):
        relaunch.prepare_relaunch(
            parent_pid=81,
            process_factory=lambda *_a, **_k: (_ for _ in ()).throw(
                OSError("spawn failed")
            ),
        )

    assert not marker.exists()
    assert relaunch._request_pending is False


def test_launch_desktop_uses_detached_canonical_command(monkeypatch):
    from osia.utils import systemd_manager

    monkeypatch.setattr(relaunch.sys, "platform", "win32")
    launched = []
    closed = []
    readiness = []
    monkeypatch.setattr(
        systemd_manager,
        "ensure_service_running",
        lambda name: readiness.append(name) or True,
    )
    monkeypatch.setattr(
        relaunch,
        "desktop_command",
        lambda: ["desktop", "--wake"],
    )
    monkeypatch.setattr(
        relaunch,
        "_detached_kwargs",
        lambda: {"detached": True},
    )
    monkeypatch.setattr(
        relaunch,
        "_close_child_streams",
        lambda kwargs: closed.append(kwargs),
    )

    result = relaunch.launch_desktop(
        process_factory=lambda command, **kwargs: launched.append(
            (command, kwargs)
        ),
    )

    assert result == 0
    assert readiness == ["osia-server.service"]
    assert launched == [(["desktop", "--wake"], {"detached": True})]
    assert closed == [{"detached": True}]


def test_launch_desktop_failure_is_nonzero_and_closes_streams(monkeypatch):
    from osia.utils import systemd_manager

    monkeypatch.setattr(relaunch.sys, "platform", "win32")
    closed = []
    monkeypatch.setattr(
        systemd_manager, "ensure_service_running", lambda _name: True,
    )
    monkeypatch.setattr(relaunch, "desktop_command", lambda: ["desktop"])
    monkeypatch.setattr(relaunch, "_detached_kwargs", lambda: {"stream": "log"})
    monkeypatch.setattr(
        relaunch,
        "_close_child_streams",
        lambda kwargs: closed.append(kwargs),
    )

    result = relaunch.launch_desktop(
        process_factory=lambda *_a, **_k: (_ for _ in ()).throw(
            OSError("spawn failed")
        ),
    )

    assert result == 5
    assert closed == [{"stream": "log"}]


def test_launch_desktop_fails_before_gui_spawn_when_server_is_not_ready(monkeypatch):
    from osia.utils import systemd_manager

    monkeypatch.setattr(relaunch.sys, "platform", "win32")
    launched = []
    monkeypatch.setattr(
        systemd_manager, "ensure_service_running", lambda _name: False,
    )
    monkeypatch.setattr(relaunch, "_detached_kwargs", lambda: {})

    result = relaunch.launch_desktop(
        process_factory=lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    assert result == 6
    assert launched == []


def test_launch_desktop_cli_dispatch(monkeypatch):
    monkeypatch.setattr(relaunch, "launch_desktop", lambda: 7)

    assert relaunch.main(["--launch-desktop"]) == 7


def test_source_start_batch_uses_detached_desktop_launcher():
    script = (ROOT / "start.bat").read_text(encoding="utf-8")

    assert '"%VENV_PY%" -m osia.relaunch --launch-desktop' in script
    assert '"%VENV_PY%" "%PROJECT_DIR%osia\\main.py" --wake' not in script


def _coordinator_fakes(monkeypatch, tmp_path, events, launched):
    monkeypatch.setattr(relaunch, "RELAUNCH_MARKER", tmp_path / "marker.json")
    monkeypatch.setattr(relaunch, "_acquire_windows_mutex", lambda *_a, **_k: "mutex")
    monkeypatch.setattr(
        relaunch,
        "_release_windows_mutex",
        lambda owned: events.append(("release", owned)),
    )
    monkeypatch.setattr(relaunch, "desktop_command", lambda: ["desktop", "--wake"])
    monkeypatch.setattr(relaunch, "_detached_kwargs", lambda: {"detached": True})
    return lambda command, **kwargs: launched.append((command, kwargs))


def test_coordinator_waits_for_parent_then_mutex_and_launches_once(
    monkeypatch, tmp_path,
):
    events = []
    launched = []
    factory = _coordinator_fakes(monkeypatch, tmp_path, events, launched)

    result = relaunch.run_coordinator(
        91,
        wait_parent=lambda pid, timeout: events.append(("parent", pid, timeout)) or True,
        wait_desktop_mutex=lambda timeout: events.append(("desktop", timeout)) or True,
        process_factory=factory,
    )

    assert result == 0
    assert events[0][0] == "parent"
    assert events[1][0] == "desktop"
    assert launched == [(["desktop", "--wake"], {"detached": True})]
    assert not relaunch.RELAUNCH_MARKER.exists()


@pytest.mark.parametrize(
    ("parent_ready", "mutex_ready", "expected"),
    [(False, True, 3), (True, False, 4)],
)
def test_coordinator_timeout_never_launches(
    monkeypatch, tmp_path, parent_ready, mutex_ready, expected,
):
    events = []
    launched = []
    factory = _coordinator_fakes(monkeypatch, tmp_path, events, launched)

    result = relaunch.run_coordinator(
        101,
        wait_parent=lambda *_a: parent_ready,
        wait_desktop_mutex=lambda *_a: mutex_ready,
        process_factory=factory,
    )

    assert result == expected
    assert launched == []
    assert not relaunch.RELAUNCH_MARKER.exists()


def test_coordinator_mutex_prevents_a_second_relauncher(
    monkeypatch, tmp_path,
):
    launched = []
    monkeypatch.setattr(relaunch, "RELAUNCH_MARKER", tmp_path / "marker.json")
    monkeypatch.setattr(relaunch.sys, "platform", "win32")
    monkeypatch.setattr(relaunch, "_acquire_windows_mutex", lambda *_a, **_k: False)

    result = relaunch.run_coordinator(
        111,
        wait_parent=lambda *_a: True,
        wait_desktop_mutex=lambda *_a: True,
        process_factory=lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    assert result == 2
    assert launched == []


def test_frozen_launcher_dispatches_relaunch_before_desktop_mutex(monkeypatch):
    launcher = _load_launcher()
    calls = []
    monkeypatch.setattr(relaunch, "run_coordinator", lambda pid: calls.append(pid) or 7)
    monkeypatch.setattr(
        launcher,
        "_acquire_gui_mutex",
        lambda: (_ for _ in ()).throw(AssertionError("desktop mutex was touched")),
    )

    assert launcher.main(["--osia-relaunch", "121"]) == 7
    assert calls == [121]
