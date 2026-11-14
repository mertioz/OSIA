"""Faster application exit keeps resource cleanup and update handoff intact."""
from __future__ import annotations

import asyncio
import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from osia import main
from osia.chatgpt_account import provider
from osia.mcp_client import manager as mcp


def test_independent_cleanups_start_together_and_all_finish_before_return():
    barrier = threading.Barrier(3)
    completed = []

    def stop(name):
        barrier.wait(timeout=3)
        completed.append(name)

    main._shutdown_independent_integrations([
        (name, lambda name=name: stop(name)) for name in ("account", "mcp", "browser")
    ])
    assert set(completed) == {"account", "mcp", "browser"}


def test_exit_waits_for_last_cleanup_and_one_failure_does_not_skip_others():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    completed = []

    def slow():
        started.set()
        assert release.wait(3)
        completed.append("slow")

    def broken():
        raise RuntimeError("fixture failure")

    def run():
        main._shutdown_independent_integrations([
            ("broken", broken), ("slow", slow),
            ("other", lambda: completed.append("other")),
        ])
        finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert started.wait(3)
        assert not finished.is_set()
    finally:
        release.set()
        worker.join(3)
    assert finished.is_set()
    assert set(completed) == {"slow", "other"}


def test_application_exit_keeps_voice_cancellation_before_integration_cleanup(monkeypatch):
    from osia.tools import browser_controller

    order = []
    monkeypatch.setattr(provider, "shutdown_account_provider", lambda: order.append("account"))
    monkeypatch.setattr(mcp, "shutdown_manager", lambda: order.append("connectors"))
    monkeypatch.setattr(browser_controller, "close_browser", lambda: order.append("browser"))
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8-sig"))
    wake = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "wake_mode")
    on_quit = next(node for node in wake.body if isinstance(node, ast.FunctionDef) and node.name == "on_quit")
    lifecycle = SimpleNamespace(close=lambda: order.append("lifecycle"))
    stop = lambda: None
    namespace = {
        "codex_update_stop": SimpleNamespace(set=lambda: order.append("updater_stop")),
        "voice_ui_lifecycle": lifecycle, "island_lifecycle": lifecycle,
        "cron_manager": SimpleNamespace(stop=stop), "unsubscribe_cron_wake": stop,
        "heartbeat_monitor": SimpleNamespace(stop=stop), "stop_manual_hotkey": None,
        "stop_popup_hotkey": None, "manual_activation_open": SimpleNamespace(clear=stop),
        "wake_gate": SimpleNamespace(cancel=lambda _: None, close=stop),
        "voice_shutdown_requested": SimpleNamespace(set=stop), "tts_stop": stop,
        "_get_active_voice_worker": lambda: None, "tts_shutdown": stop,
        "_shutdown_voice_runtime": lambda **_: order.append("voice_closed") or True,
        "_shutdown_independent_integrations": main._shutdown_independent_integrations,
    }
    exec(compile(ast.Module(body=[on_quit], type_ignores=[]), "<actual exit callback>", "exec"), namespace)
    namespace["on_quit"]()
    assert order[:4] == ["updater_stop", "lifecycle", "lifecycle", "voice_closed"]
    assert set(order[4:]) == {"account", "connectors", "browser"}


@pytest.mark.parametrize(("module", "slot", "constructor", "shutdown"), [
    (provider, "_PROVIDER", "ChatGPTAccountProvider", "shutdown_account_provider"),
    (mcp, "_manager_singleton", "MCPManager", "shutdown_manager"),
])
def test_unused_subsystems_are_not_created_at_exit(monkeypatch, module, slot, constructor, shutdown):
    monkeypatch.setattr(module, slot, None)
    monkeypatch.setattr(module, constructor, lambda: pytest.fail("created during exit"))
    getattr(module, shutdown)()
    calls = []
    existing = SimpleNamespace(shutdown=lambda: calls.append("closed"))
    monkeypatch.setattr(module, slot, existing)
    getattr(module, shutdown)()
    assert calls == ["closed"]
    assert getattr(module, slot) is existing


def test_all_mcp_transports_begin_graceful_cleanup_before_waiting():
    manager = mcp.MCPManager()
    loop = manager._ensure_loop()
    original_thread = manager._thread
    closed, cancelled = [], []
    all_stopping = None
    ready = [threading.Event() for _ in range(3)]

    async def serve(conn, initialized):
        nonlocal all_stopping
        if all_stopping is None:
            all_stopping = asyncio.Event()
        conn.stop_event = asyncio.Event()
        initialized.set()
        try:
            await conn.stop_event.wait()
            closed.append(conn.server_id)
            if len(closed) == 3:
                all_stopping.set()
            await all_stopping.wait()
        except asyncio.CancelledError:
            cancelled.append(conn.server_id)
            raise
        finally:
            conn.done_event.set()

    for index in range(3):
        conn = mcp._ServerConn(server_id=str(index), cfg={}, status=mcp.STATUS_CONNECTED)
        manager._servers[conn.server_id] = conn
        conn.serve_future = asyncio.run_coroutine_threadsafe(serve(conn, ready[index]), loop)
    try:
        assert all(event.wait(3) for event in ready)
        manager.shutdown(timeout=0.2)
        assert set(closed) == {"0", "1", "2"}
        assert cancelled == []
        assert not original_thread.is_alive()
        assert manager.servers() == {}
        assert manager._ensure_loop() is not None
    finally:
        manager.shutdown(timeout=0.2)


@pytest.mark.parametrize("error", [None, RuntimeError("fixture")])
def test_installer_handoff_quits_on_next_event_only_after_success(monkeypatch, error):
    from osia.gui import update_ui

    scheduled, failed = [], []
    fake_app = SimpleNamespace(quit=lambda: None)
    monkeypatch.setattr(update_ui, "QApplication", SimpleNamespace(instance=lambda: fake_app))
    monkeypatch.setattr(update_ui, "QTimer", SimpleNamespace(
        singleShot=lambda delay, callback: scheduled.append((delay, callback)),
    ))
    state = SimpleNamespace(
        operation=SimpleNamespace(snapshot=SimpleNamespace(phase="launching"), fail=failed.append),
        _publish=lambda **_kwargs: None,
    )
    update_ui.UpdateController._on_launch_finished(state, error)
    if error:
        assert failed
        assert not scheduled
    else:
        assert not failed
        assert scheduled == [(0, fake_app.quit)]
