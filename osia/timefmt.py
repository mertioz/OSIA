#!/usr/bin/env python3
"""OSIA time formatting — render wall-clock times per the 12h/24h setting.

A tiny, dependency-free helper used wherever OSIA shows a clock time
(conversation timestamps, cron daily schedules, heartbeat alert logs). The
format is read from ``config/settings.json`` (key ``"time_format"``: one of
``"24h"`` — e.g. ``14:30`` — or ``"12h"`` — e.g. ``2:30 PM``; default
``"24h"``) and re-read on every call, so toggling it in the Settings GUI
takes effect immediately.
"""

from datetime import datetime

from osia.config import get_setting


def use_12h() -> bool:
    """True when the user selected the 12-hour clock, False for 24-hour."""
    return str(get_setting("time_format", "24h")).strip().lower() == "12h"


def format_hm(hour: int, minute: int) -> str:
    """Format a 24-hour ``(hour, minute)`` pair as ``"14:30"`` or ``"2:30 PM"``."""
    if use_12h():
        suffix = "AM" if hour < 12 else "PM"
        h12 = hour % 12 or 12
        return f"{h12}:{minute:02d} {suffix}"
    return f"{hour:02d}:{minute:02d}"


def format_time(dt: datetime) -> str:
    """Format a ``datetime``'s wall-clock time per the user's setting."""
    return format_hm(dt.hour, dt.minute)
