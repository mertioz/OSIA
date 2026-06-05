#!/usr/bin/env python3
"""OSIA scheduled tasks (cron) — Bloc 5.

A small, dependency-free scheduler that runs task descriptions through the
agent on a recurring schedule, exactly as if the user had spoken them.

The job list lives in ``config/settings.json`` under ``cron_jobs`` as a list
of ``{"task": <description>, "schedule": <spec>, "enabled": <bool>,
"conversation_id": <id|null>}`` entries. ``schedule`` is one of:

  * ``"every <N> minutes"`` / ``"every <N> hours"`` / ``"every <N> days"``
  * ``"at HH:MM"`` — fires once a day at that local wall-clock time
  * ``"once:YYYY-MM-DDTHH:MM"`` — fires exactly once at that local date+time,
    then the entry is auto-disabled (``enabled: false``) and persisted

The manager polls the settings on every tick, so jobs added / edited / toggled
in the Settings GUI take effect without restarting OSIA. A single background
daemon thread drives everything; :meth:`CronManager.start` and
:meth:`CronManager.stop` bracket its lifetime and shut it down cleanly.

This module is intentionally decoupled from the agent: the caller passes a
``run_task(description: str, conversation_id: str | None) -> None`` callback
(see ``osia/main.py``) that actually executes the task and speaks the reply.
When a job is linked to a conversation, its id is passed through so the
callback can load that conversation's history and keep context across runs.
The manager only decides *when* to call it.

All log lines are prefixed ``[CRON]``.
"""

import re
import threading
from datetime import datetime, timedelta
from typing import Callable

from osia.config import get_setting, set_setting

# How often the scheduler wakes to re-evaluate the job list. 20s keeps
# "at HH:MM" jobs accurate to within one tick while staying cheap.
DEFAULT_TICK_SECONDS = 20.0

_UNIT_SECONDS = {
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


def parse_schedule(spec: str) -> dict | None:
    """Parse a schedule string into a normalized descriptor.

    Returns one of::

        {"kind": "interval", "seconds": <int>}     # "every N minutes/hours/days"
        {"kind": "daily", "hour": <int>, "minute": <int>}   # "at HH:MM"
        {"kind": "once", "dt": <datetime>}    # "once:YYYY-MM-DDTHH:MM"

    or ``None`` when the spec is empty / unrecognised (the job is then skipped,
    not crashed).
    """
    if not isinstance(spec, str):
        return None
    s = spec.strip().lower()
    if not s:
        return None

    # "once:YYYY-MM-DDTHH:MM" — fire exactly once at this local date+time, then
    # auto-disable. The 'T' separator is lower-cased to 't' by ``.lower()`` above.
    m = re.match(r"^once:(\d{4})-(\d{2})-(\d{2})t([01]?\d|2[0-3]):([0-5]\d)$", s)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)),
            )
        except ValueError:
            return None  # impossible calendar date (e.g. 31/02)
        return {"kind": "once", "dt": dt}

    # "every <N> <unit>(s)"  — unit ∈ minute/hour/day (singular or plural).
    m = re.match(r"^every\s+(\d+)\s*(minute|hour|day)s?$", s)
    if m:
        n = int(m.group(1))
        if n <= 0:
            return None
        return {"kind": "interval", "seconds": n * _UNIT_SECONDS[m.group(2)]}

    # "at HH:MM" — daily wall-clock time (24h). Also accept a bare "HH:MM".
    m = re.match(r"^(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)$", s)
    if m:
        return {"kind": "daily", "hour": int(m.group(1)), "minute": int(m.group(2))}

    # "at H:MM am/pm" — daily 12-hour time (the GUI emits this when the user's
    # time_format is "12h"). Stored hour/minute stay 24h internally.
    m = re.match(r"^(?:at\s+)?(1[0-2]|0?[1-9]):([0-5]\d)\s*(am|pm)$", s)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "pm":
            hour += 12
        return {"kind": "daily", "hour": hour, "minute": int(m.group(2))}

    return None


def _job_signature(job: dict) -> tuple:
    """Stable identity for a job slot, used to detect in-place edits."""
    return (str(job.get("task", "")), str(job.get("schedule", "")))


class CronManager:
    """Background scheduler for OSIA's user-defined recurring tasks.

    Parameters
    ----------
    run_task:
        Callback invoked with the job's task description (and the linked
        conversation id, or ``None``) when a job fires. It runs on a dedicated
        worker thread so a slow task never stalls the scheduler. Exceptions
        raised by it are logged, never propagated.
    tick_seconds:
        Poll interval. Lower = more accurate "at HH:MM" firing, at a tiny
        CPU cost.
    """

    def __init__(self, run_task: Callable[..., None],
                 tick_seconds: float = DEFAULT_TICK_SECONDS):
        self._run_task = run_task
        self._tick = max(1.0, float(tick_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # index -> {"sig": tuple, "last_run": datetime|None, "last_date": date|None}
        self._state: dict[int, dict] = {}
        # True while a fired task is still executing — prevents a long task
        # from overlapping with the next one it would otherwise trigger.
        self._busy = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="osia-cron", daemon=True
        )
        self._thread.start()
        print("[CRON] scheduler started")

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the scheduler to stop and wait briefly for it to exit."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        print("[CRON] scheduler stopped")

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_jobs(datetime.now())
            except Exception as e:  # never let a bad job kill the scheduler
                print(f"[CRON] tick error: {e}")
            # Event.wait returns early (True) when stop() is called, so the
            # thread shuts down promptly instead of sleeping out the tick.
            self._stop.wait(self._tick)

    def _check_jobs(self, now: datetime) -> None:
        jobs = get_setting("cron_jobs", []) or []
        if not isinstance(jobs, list):
            return

        seen_indices = set()
        for idx, job in enumerate(jobs):
            if not isinstance(job, dict):
                continue
            seen_indices.add(idx)
            sig = _job_signature(job)
            st = self._state.get(idx)
            # A new slot, or one whose task/schedule was edited, starts fresh:
            # this means it won't immediately fire on startup or right after
            # an edit (it waits a full interval / the next scheduled time).
            if st is None or st["sig"] != sig:
                st = self._init_state(sig, job, now)
                self._state[idx] = st

            if not job.get("enabled", False):
                continue

            if not str(job.get("task", "")).strip():
                continue  # incomplete row — nothing to run

            spec = parse_schedule(job.get("schedule", ""))
            if spec is None:
                continue

            if self._is_due(spec, st, now):
                launched = self._fire(
                    job.get("task", ""), job.get("schedule", ""), st, now,
                    conversation_id=job.get("conversation_id"))
                # A one-shot job runs exactly once: as soon as it actually
                # launches, flag it in-memory (so it never re-fires this
                # session even if the persist below fails) and disable it on
                # disk so it stays off across restarts.
                if launched and spec["kind"] == "once":
                    st["once_fired"] = True
                    self._disable_once_job(idx, sig)

        # Drop state for slots that no longer exist (jobs were removed).
        for stale in [i for i in self._state if i not in seen_indices]:
            del self._state[stale]

    @staticmethod
    def _init_state(sig: tuple, job: dict, now: datetime) -> dict:
        """Seed a job slot's timing so it doesn't fire immediately.

        Interval jobs anchor ``last_run`` at ``now`` (first fire one interval
        later). Daily jobs mark today as already-fired when ``now`` is already
        past today's scheduled time, so OSIA never "catches up" a missed run on
        startup.
        """
        st = {"sig": sig, "last_run": now, "last_date": None}
        spec = parse_schedule(job.get("schedule", ""))
        if spec and spec["kind"] == "daily":
            scheduled = now.replace(
                hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0
            )
            if now >= scheduled:
                st["last_date"] = now.date()
        return st

    @staticmethod
    def _is_due(spec: dict, st: dict, now: datetime) -> bool:
        if spec["kind"] == "interval":
            last = st.get("last_run")
            if last is None:
                return True
            return (now - last) >= timedelta(seconds=spec["seconds"])
        if spec["kind"] == "daily":
            if st.get("last_date") == now.date():
                return False
            scheduled = now.replace(
                hour=spec["hour"], minute=spec["minute"], second=0, microsecond=0
            )
            return now >= scheduled
        if spec["kind"] == "once":
            # Fire once when the target time is reached (or already past, e.g.
            # OSIA started after it), then never again this session.
            if st.get("once_fired"):
                return False
            return now >= spec["dt"]
        return False

    def _fire(self, task: str, schedule: str, st: dict, now: datetime,
              conversation_id: str | None = None) -> bool:
        """Launch a job's task on a worker thread.

        Returns ``True`` when the task was actually launched (timing state
        committed), ``False`` when it was skipped — empty task or the scheduler
        is busy with another run. Callers use this to know whether a one-shot
        job has run and may now be disabled.
        """
        task = (task or "").strip()
        if not task:
            return False
        # If another scheduled task is still running, leave this job's timing
        # state UNTOUCHED so it stays "due" and retries on the next tick. We must
        # NOT mark it done here: a daily "at HH:MM" run blocked at its fire time
        # would otherwise be silently lost for the rest of the day.
        if self._busy.is_set():
            print(f"[CRON] busy — deferring '{task[:60]}' to the next tick")
            return False

        # Commit the timing state only now that we are actually launching. The
        # scheduler loop is single-threaded, so this completes before any later
        # tick and cannot double-fire.
        st["last_run"] = now
        st["last_date"] = now.date()

        print(f"[CRON] firing scheduled task ({schedule}): {task[:80]}")

        def _worker():
            self._busy.set()
            try:
                self._run_task(task, conversation_id)
            except Exception as e:
                print(f"[CRON] task failed: {e}")
            finally:
                self._busy.clear()

        threading.Thread(target=_worker, name="osia-cron-task", daemon=True).start()
        return True

    def _disable_once_job(self, idx: int, sig: tuple) -> None:
        """Persist ``enabled: false`` for the one-shot job at ``idx`` so it does
        not fire again after a restart. Matches on signature so a concurrent
        reorder/edit in the GUI never disables the wrong row."""
        try:
            jobs = get_setting("cron_jobs", []) or []
            if not isinstance(jobs, list) or not (0 <= idx < len(jobs)):
                return
            job = jobs[idx]
            if not isinstance(job, dict) or _job_signature(job) != sig:
                return
            job["enabled"] = False
            set_setting("cron_jobs", jobs)
            print(f"[CRON] one-shot complete — disabled: "
                  f"{str(job.get('task', ''))[:60]}")
        except Exception as e:
            print(f"[CRON] could not disable one-shot job: {e}")
