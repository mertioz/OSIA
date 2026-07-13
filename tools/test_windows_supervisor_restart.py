#!/usr/bin/env python3
"""Deterministic WOR-114 tests; no real task or service command is executed."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.utils import systemd_manager as manager
from windows import run_service as supervisor


class FakeShutdown:
    def __init__(self, trigger_wait: int, on_trigger=None):
        self.trigger_wait = trigger_wait
        self.on_trigger = on_trigger
        self.wait_calls = 0
        self.requested = False

    def is_set(self):
        return self.requested

    def wait(self, _timeout):
        self.wait_calls += 1
        if self.wait_calls >= self.trigger_wait:
            self.requested = True
            if self.on_trigger is not None:
                callback, self.on_trigger = self.on_trigger, None
                callback()
        return self.requested


class FakeMutex:
    def __init__(self, events=None):
        self.events = events
        self.acquired = False
        self.abandoned = False

    def acquire(self, _timeout):
        self.acquired = True
        if self.events is not None:
            self.events.append("lifecycle-acquire")
        return True

    def release(self):
        if self.acquired and self.events is not None:
            self.events.append("lifecycle-release")
        self.acquired = False

    def close(self):
        self.release()


class FakeJob:
    def __init__(self, assign_ok=True, assign_error=None):
        self.assign_ok = assign_ok
        self.assign_error = assign_error
        self.assigned = []

    def assign(self, child):
        self.assigned.append(child.pid)
        if self.assign_error is not None:
            raise self.assign_error
        return self.assign_ok


class FakeChild:
    def __init__(self, pid, rc=None):
        self.pid = pid
        self.rc = rc
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminate_calls += 1
        self.rc = 0

    def kill(self):
        self.kill_calls += 1
        self.rc = 1

    def wait(self, timeout=None):
        if self.rc is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.rc


class SupervisorLoopTests(unittest.TestCase):
    def _run(self, shutdown, children, job=None):
        job = job or FakeJob()
        with (
            patch.object(supervisor.subprocess, "Popen", side_effect=children) as popen,
            patch.object(supervisor, "_other_owner_running", return_value=False),
            patch.object(supervisor, "_port_listening", return_value=False),
            patch.object(supervisor, "_say"),
        ):
            result = supervisor._supervise(
                "server", ["python", "run_server.py"], str(ROOT), None,
                Path("child.log"), shutdown, FakeMutex(), job,
            )
        return result, popen, job

    def test_shutdown_while_child_running_stops_child_without_respawn(self):
        child = FakeChild(101)
        result, popen, job = self._run(FakeShutdown(1), [child])
        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(job.assigned, [101])
        self.assertEqual(child.terminate_calls, 1)

    def test_expected_child_exit_during_shutdown_does_not_restart(self):
        child = FakeChild(102)
        shutdown = FakeShutdown(1, on_trigger=lambda: setattr(child, "rc", 0))
        result, popen, _job = self._run(shutdown, [child])
        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(child.terminate_calls, 0)

    def test_unexpected_child_crash_still_restarts(self):
        crashed = FakeChild(103, rc=7)
        replacement = FakeChild(104)
        result, popen, job = self._run(FakeShutdown(3), [crashed, replacement])
        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(job.assigned, [103, 104])
        self.assertEqual(replacement.terminate_calls, 1)

    def test_partial_launch_ownership_failure_leaves_no_orphan(self):
        child = FakeChild(105)
        result, popen, _job = self._run(
            FakeShutdown(99), [child], job=FakeJob(assign_ok=False),
        )
        self.assertEqual(result, 1)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(child.terminate_calls, 1)

    def test_job_assignment_exception_leaves_no_orphan(self):
        child = FakeChild(106)
        result, popen, _job = self._run(
            FakeShutdown(99), [child],
            job=FakeJob(assign_error=OSError("simulated assignment failure")),
        )
        self.assertEqual(result, 1)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(child.terminate_calls, 1)


class StopBarrierTests(unittest.TestCase):
    def setUp(self):
        self.tree = manager._WinServiceTree(
            processes={201: 1.0, 202: 2.0}, supervisors={201},
        )

    def _state(self, *, task=False, alive=frozenset(), listeners=frozenset(),
               unowned=frozenset()):
        with (
            patch.object(manager, "_win_collect_supervised_tree",
                         side_effect=lambda _kind, tree: tree),
            patch.object(manager, "_win_task_running_for_barrier", return_value=task),
            patch.object(manager, "_win_owned_alive_pids", return_value=set(alive)),
            patch.object(manager, "_win_listener_pids", return_value=set(listeners)),
            patch.object(manager, "_win_unowned_service_pids",
                         return_value=set(unowned)),
        ):
            return manager._win_stop_barrier_state(
                "OSIA Server", "server", self.tree,
            )

    def test_old_task_or_supervisor_delay_blocks_start_barrier(self):
        ready, state = self._state(task=True)
        self.assertFalse(ready)
        self.assertEqual(state["task_running"], 1)
        ready, state = self._state(alive={201})
        self.assertFalse(ready)
        self.assertEqual(state["supervisors_alive"], 1)

    def test_old_child_delay_blocks_start_barrier(self):
        ready, state = self._state(alive={202})
        self.assertFalse(ready)
        self.assertEqual(state["descendants_alive"], 1)

    def test_listener_release_delay_blocks_start_barrier(self):
        ready, state = self._state(listeners={202})
        self.assertFalse(ready)
        self.assertEqual(state["old_listeners"], 1)

    def test_unowned_osia_owner_fails_closed_but_unrelated_python_does_not(self):
        ready, state = self._state(unowned={999})
        self.assertFalse(ready)
        self.assertEqual(state["unowned_owners"], 1)
        ready, _state = self._state()
        self.assertTrue(ready)


class CoordinatedRestartTests(unittest.TestCase):
    def _lifecycle_patches(self, events, wait_result):
        class Signal:
            def __init__(self, _kind):
                events.append("shutdown-signal")

            def close(self):
                events.append("shutdown-close")

        return (
            patch.object(manager, "_WinNamedMutex",
                         side_effect=lambda _name: FakeMutex(events)),
            patch.object(manager, "_WinShutdownSignal", Signal),
            patch.object(manager, "_win_collect_supervised_tree",
                         side_effect=lambda _kind, tree: tree),
            patch.object(manager, "_win_wait_stop_barrier",
                         side_effect=wait_result),
            patch.object(manager, "_win_task_registered", return_value=True),
        )

    def test_success_starts_exactly_once_after_complete_barrier(self):
        events = []

        def barrier(*_args):
            self.assertNotIn("start", events)
            events.append("barrier-complete")
            return True, {}

        patches = self._lifecycle_patches(events, barrier)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
            manager, "_win_run_task",
            side_effect=lambda _task: events.append("start") or True,
        ) as run_task:
            self.assertTrue(manager._win_coordinated_restart("server"))
        self.assertEqual(run_task.call_count, 1)
        self.assertLess(events.index("barrier-complete"), events.index("shutdown-close"))
        self.assertLess(events.index("shutdown-close"), events.index("start"))

    def test_stop_barrier_timeout_never_starts_replacement(self):
        events = []
        timeout_state = {"descendants_alive": 1}
        patches = self._lifecycle_patches(
            events, [(False, timeout_state), (False, timeout_state)],
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
            manager, "_win_force_stop_owned",
            side_effect=lambda *_args: events.append("forced-stop"),
        ), patch.object(manager, "_win_run_task") as run_task:
            self.assertFalse(manager._win_coordinated_restart("server"))
        run_task.assert_not_called()
        self.assertIn("forced-stop", events)

    def test_barrier_probe_failure_forces_stop_and_never_starts(self):
        events = []
        patches = self._lifecycle_patches(
            events, RuntimeError("simulated listener probe failure"),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
            manager, "_win_force_stop_owned",
            side_effect=lambda *_args: events.append("forced-stop"),
        ), patch.object(manager, "_win_run_task") as run_task:
            self.assertFalse(manager._win_coordinated_restart("server"))
        run_task.assert_not_called()
        self.assertIn("forced-stop", events)

    def test_concurrent_restart_calls_are_serialized(self):
        active = 0
        maximum = 0
        lock = threading.Lock()
        entered = threading.Event()

        def fake_restart(_kind):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                entered.set()
            time.sleep(0.05)
            with lock:
                active -= 1
            return True

        results = []
        with patch.object(manager, "_win_coordinated_restart", side_effect=fake_restart):
            first = threading.Thread(
                target=lambda: results.append(manager.restart_service("osia-server.service")),
            )
            second = threading.Thread(
                target=lambda: results.append(manager.restart_service("osia-server.service")),
            )
            first.start()
            self.assertTrue(entered.wait(1.0))
            second.start()
            first.join(2.0)
            second.join(2.0)
        self.assertEqual(results, [True, True])
        self.assertEqual(maximum, 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows named mutex semantics")
    def test_abandoned_mutex_ownership_is_recoverable(self):
        name = f"Local\\OSIA.Test.Abandoned.{uuid.uuid4()}"
        owners = []

        def abandon():
            mutex = manager._WinNamedMutex(name)
            self.assertTrue(mutex.acquire(1.0))
            owners.append(mutex)
            # Thread exit abandons ownership while the retained handle keeps the
            # named kernel object alive for the next waiter.

        thread = threading.Thread(target=abandon)
        thread.start()
        thread.join(2.0)
        contender = manager._WinNamedMutex(name)
        try:
            self.assertTrue(contender.acquire(1.0))
            self.assertTrue(contender.abandoned)
        finally:
            contender.close()
            owners[0].close()

    def test_repeated_restart_invocations_are_safe(self):
        calls = []
        with patch.object(
            manager, "_win_coordinated_restart",
            side_effect=lambda kind: calls.append(kind) or True,
        ):
            self.assertTrue(manager.restart_service("osia-server.service"))
            self.assertTrue(manager.restart_service("osia-server.service"))
        self.assertEqual(calls, ["server", "server"])

    def test_non_windows_restart_path_is_unchanged(self):
        result = SimpleNamespace(returncode=0)
        with patch.object(manager, "_IS_WINDOWS", False), patch.object(
            manager, "_run", return_value=result,
        ) as run:
            self.assertTrue(manager.restart_service("osia-server.service"))
        run.assert_called_once_with(
            ["pkexec", "systemctl", "restart", "osia-server.service"],
            timeout=60.0,
        )


class OwnershipTargetingTests(unittest.TestCase):
    def test_unrelated_python_process_is_never_targeted(self):
        tracked = Mock(pid=301)
        unrelated = Mock(pid=302)
        fake_psutil = SimpleNamespace(
            NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
            AccessDenied=type("AccessDenied", (Exception,), {}),
            wait_procs=Mock(return_value=([tracked], [])),
        )

        def identity(pid, _created):
            return tracked if pid == 301 else None

        with patch.object(
            manager, "_win_process_for_identity", side_effect=identity,
        ), patch.dict(sys.modules, {"psutil": fake_psutil}):
            manager._win_terminate_identities({301: 1.0}, timeout=0.1)
        tracked.terminate.assert_called_once_with()
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
