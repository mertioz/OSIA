#!/usr/bin/env python3
"""Wait-and-recheck helper for long-running tool actions (WOR-31).

Some tool actions are not instantaneous: installing packages, cloning a
repo, or launching a browser on a slow machine all take seconds. Without a
wait-and-recheck step the agent either blocks blindly or reads a premature
result and moves on, claiming success before the action actually finished.

``wait_and_verify`` polls a caller-supplied ``check_fn`` until it returns a
truthy value or a timeout elapses, returning ``(success, elapsed_seconds)``
so callers can both branch on the outcome and report how long it took. It is
deliberately dependency-free and side-effect-free (beyond a debug print) so
it can be reused from any tool on Linux, Windows, and Mac.
"""

from __future__ import annotations

import time
from typing import Callable


def wait_and_verify(
    check_fn: Callable[[], object],
    timeout_s: float,
    poll_interval_s: float = 1.0,
    description: str = "",
) -> tuple[bool, float]:
    """Poll ``check_fn`` until it returns truthy or ``timeout_s`` elapses.

    Parameters
    ----------
    check_fn:
        Zero-argument callable re-invoked on each poll. Any truthy return
        value means "done / verified" and ends the wait successfully. A
        callable that raises is treated as "not ready yet" (the exception is
        swallowed) so a transient probe error never aborts the wait early.
    timeout_s:
        Maximum wall-clock time to wait, in seconds.
    poll_interval_s:
        Delay between polls, in seconds (default 1.0). Clamped to a small
        positive minimum so a zero/negative value never busy-loops.
    description:
        Optional short label used only for debug logging.

    Returns
    -------
    (success, elapsed):
        ``success`` is True if ``check_fn`` returned truthy before the
        timeout, False otherwise. ``elapsed`` is the wall-clock seconds spent
        waiting (monotonic), regardless of outcome.
    """
    started = time.monotonic()
    interval = max(0.05, float(poll_interval_s))
    deadline = started + max(0.0, float(timeout_s))

    while True:
        try:
            if check_fn():
                elapsed = time.monotonic() - started
                if description:
                    print(f"[task_wait] ✓ {description} ready after {elapsed:.1f}s")
                return True, elapsed
        except Exception:
            # A probe that raises just means "not ready yet" — keep waiting.
            pass

        now = time.monotonic()
        if now >= deadline:
            elapsed = now - started
            if description:
                print(
                    f"[task_wait] ✗ {description} not ready after "
                    f"{elapsed:.1f}s (timeout {timeout_s:.0f}s)"
                )
            return False, elapsed

        # Don't overshoot the deadline on the final sleep.
        time.sleep(min(interval, max(0.0, deadline - now)))
