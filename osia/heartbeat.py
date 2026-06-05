#!/usr/bin/env python3
"""OSIA heartbeat — system monitoring (Bloc 5).

A background daemon thread that samples the machine on a fixed interval and
speaks a TTS alert when a monitored metric crosses its configured threshold:

  * CPU usage  — alert when usage % is above ``cpu.threshold``
  * RAM usage  — alert when usage % is above ``ram.threshold``
  * Disk space — alert when *free* % is below ``disk.threshold``
  * Network    — alert when a connectivity probe fails

Everything is driven from ``config/settings.json`` and re-read on every cycle,
so toggling the monitor, changing the interval, or editing a threshold in the
Settings GUI takes effect without restarting OSIA:

  * ``heartbeat_enabled``            — master on/off
  * ``heartbeat_interval_minutes``   — sampling period (minutes)
  * ``heartbeat_checks``             — per-metric ``{enabled, threshold}``

Alerts are *edge-triggered*: a metric alerts once when it first crosses its
threshold and stays silent until it recovers, so a sustained breach doesn't
repeat every cycle. The caller supplies an ``alert(message: str) -> None``
callback (see ``osia/main.py``) that speaks the already-localized message.

All log lines are prefixed ``[HEARTBEAT]``.
"""

import socket
import threading
import time
from datetime import datetime
from typing import Callable

from osia.config import get_setting
from osia import i18n, timefmt

# Base poll cadence. The monitor wakes this often to stay responsive to a
# live enable/disable or interval change, but only *samples* the machine once
# a full ``heartbeat_interval_minutes`` has elapsed since the last sample.
_POLL_SECONDS = 15.0

# Connectivity probe target (Cloudflare DNS) + timeout. A TCP connect to a
# well-known resolver is a cheap, dependency-free "is the internet up" check.
_NET_PROBE_HOST = "1.1.1.1"
_NET_PROBE_PORT = 53
_NET_PROBE_TIMEOUT = 3.0


class HeartbeatMonitor:
    """Periodic system-health monitor that speaks threshold-breach alerts.

    Parameters
    ----------
    alert:
        Callback invoked with a fully-localized alert message when a metric
        crosses its threshold. Runs on the monitor thread; exceptions raised
        by it are logged, never propagated.
    """

    def __init__(self, alert: Callable[[str], None]):
        self._alert = alert
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_check = 0.0
        # Per-metric edge state: True while currently in the alerted condition.
        self._alerted = {"cpu": False, "ram": False, "disk": False, "network": False}

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the monitor thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="osia-heartbeat", daemon=True
        )
        self._thread.start()
        print("[HEARTBEAT] monitor started")

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the monitor to stop and wait briefly for it to exit."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        print("[HEARTBEAT] monitor stopped")

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if get_setting("heartbeat_enabled", False):
                    interval = self._interval_seconds()
                    now = time.monotonic()
                    if now - self._last_check >= interval:
                        self._last_check = now
                        self._run_checks()
                else:
                    # Disabled: forget edge state so re-enabling starts clean.
                    self._reset_alerts()
            except Exception as e:  # a bad sample must never kill the monitor
                print(f"[HEARTBEAT] cycle error: {e}")
            self._stop.wait(_POLL_SECONDS)

    @staticmethod
    def _interval_seconds() -> float:
        try:
            minutes = float(get_setting("heartbeat_interval_minutes", 5) or 5)
        except (TypeError, ValueError):
            minutes = 5.0
        return max(1.0, minutes) * 60.0

    def _reset_alerts(self) -> None:
        for k in self._alerted:
            self._alerted[k] = False

    def _run_checks(self) -> None:
        checks = get_setting("heartbeat_checks", {}) or {}
        if not isinstance(checks, dict):
            return
        self._check_cpu(checks.get("cpu") or {})
        self._check_ram(checks.get("ram") or {})
        self._check_disk(checks.get("disk") or {})
        self._check_network(checks.get("network") or {})

    # ── Individual metrics ────────────────────────────────────────────────

    def _check_cpu(self, cfg: dict) -> None:
        if not cfg.get("enabled", False):
            self._alerted["cpu"] = False
            return
        try:
            import psutil
            usage = psutil.cpu_percent(interval=1.0)
        except Exception as e:
            print(f"[HEARTBEAT] CPU sample failed: {e}")
            return
        threshold = _num(cfg.get("threshold"), 80)
        print(f"[HEARTBEAT] CPU {usage:.0f}% (threshold {threshold:.0f}%)")
        self._evaluate(
            "cpu", usage > threshold,
            lambda: i18n.t("heartbeat.alert_cpu",
                           value=int(round(usage)), threshold=int(round(threshold))),
        )

    def _check_ram(self, cfg: dict) -> None:
        if not cfg.get("enabled", False):
            self._alerted["ram"] = False
            return
        try:
            import psutil
            usage = psutil.virtual_memory().percent
        except Exception as e:
            print(f"[HEARTBEAT] RAM sample failed: {e}")
            return
        threshold = _num(cfg.get("threshold"), 90)
        print(f"[HEARTBEAT] RAM {usage:.0f}% (threshold {threshold:.0f}%)")
        self._evaluate(
            "ram", usage > threshold,
            lambda: i18n.t("heartbeat.alert_ram",
                           value=int(round(usage)), threshold=int(round(threshold))),
        )

    def _check_disk(self, cfg: dict) -> None:
        if not cfg.get("enabled", False):
            self._alerted["disk"] = False
            return
        try:
            import psutil
            free_pct = 100.0 - psutil.disk_usage("/").percent
        except Exception as e:
            print(f"[HEARTBEAT] disk sample failed: {e}")
            return
        threshold = _num(cfg.get("threshold"), 10)
        print(f"[HEARTBEAT] disk {free_pct:.0f}% free (threshold {threshold:.0f}%)")
        self._evaluate(
            "disk", free_pct < threshold,
            lambda: i18n.t("heartbeat.alert_disk",
                           value=int(round(free_pct)), threshold=int(round(threshold))),
        )

    def _check_network(self, cfg: dict) -> None:
        if not cfg.get("enabled", False):
            self._alerted["network"] = False
            return
        up = self._network_up()
        print(f"[HEARTBEAT] network {'up' if up else 'DOWN'}")
        self._evaluate(
            "network", not up,
            lambda: i18n.t("heartbeat.alert_network"),
        )

    @staticmethod
    def _network_up() -> bool:
        try:
            with socket.create_connection(
                (_NET_PROBE_HOST, _NET_PROBE_PORT), timeout=_NET_PROBE_TIMEOUT
            ):
                return True
        except OSError:
            return False

    # ── Edge-triggered alerting ───────────────────────────────────────────

    def _evaluate(self, name: str, breached: bool, message_fn) -> None:
        """Fire ``alert`` only on the OK→breach transition; clear on recovery."""
        if breached and not self._alerted[name]:
            self._alerted[name] = True
            try:
                msg = message_fn()
                print(f"[HEARTBEAT] {timefmt.format_time(datetime.now())} ALERT: {msg}")
                self._alert(msg)
            except Exception as e:
                print(f"[HEARTBEAT] alert delivery failed: {e}")
        elif not breached:
            self._alerted[name] = False


def _num(value, default: float) -> float:
    """Coerce a settings value to float, falling back to ``default``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
