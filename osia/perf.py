#!/usr/bin/env python3
"""Lightweight, opt-in performance instrumentation for OSIA.

Disabled by default — set the ``PERF_DEBUG`` environment variable (to anything
other than ``""``/``0``/``false``/``no``/``off``) to print timing lines, e.g.::

    PERF_DEBUG=1 python osia/main.py --wake      # Linux/Mac
    $env:PERF_DEBUG=1; python osia\\main.py --wake  # Windows PowerShell

When disabled, ``perf_timer`` and ``perf_mark`` are effectively no-ops (a single
boolean check), so the instrumentation can stay sprinkled in production hot
paths at zero cost. Added to diagnose the Windows startup / click / settings
latency (WOR perf fix) and kept for future profiling.
"""

import contextlib
import os
import time

# Read once at import — PERF_DEBUG is meant to be set before launch.
_FLAG = os.environ.get("PERF_DEBUG", "").strip().lower()
PERF_DEBUG = _FLAG not in ("", "0", "false", "no", "off")

# Timestamp of the previous perf_mark(), for printing deltas during the
# single-threaded startup sequence.
_last_mark: list = [None]


def perf_enabled() -> bool:
    """True when PERF_DEBUG instrumentation is active."""
    return PERF_DEBUG


@contextlib.contextmanager
def perf_timer(label: str):
    """Context manager that prints how long the wrapped block took.

    No-op (yields immediately) when PERF_DEBUG is unset.
    """
    if not PERF_DEBUG:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        dt = (time.perf_counter() - start) * 1000.0
        print(f"[PERF] {label}: {dt:.1f} ms", flush=True)


def perf_mark(label: str) -> None:
    """Print a labelled checkpoint and the delta since the previous mark.

    Useful for tracing a linear startup sequence. No-op when PERF_DEBUG is
    unset.
    """
    if not PERF_DEBUG:
        return
    now = time.perf_counter()
    prev = _last_mark[0]
    if prev is None:
        print(f"[PERF] mark {label} (start)", flush=True)
    else:
        print(f"[PERF] mark {label}: +{(now - prev) * 1000.0:.1f} ms", flush=True)
    _last_mark[0] = now
